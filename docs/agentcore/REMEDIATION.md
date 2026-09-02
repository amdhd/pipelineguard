# Remediation plan — findings from the pre-Phase-3 audit

**Written 2026-09-01, after Phase 2 shipped and produced two real PRs — one
merged, one closed as wrong.** This document exists because the second one was
worth more than the first, and the audit it prompted turned up problems that
`PLAN.md` believes are solved.

`PLAN.md` is the design. `EVIDENCE.md` is what measurement showed. This is the
gap between them, with a fix for each item and a way to tell whether the fix
worked. Items are ordered by what they block, not by severity — a small problem
that blocks a decision outranks a large one that does not.

**Decision: CAUTION at the time of writing; both P0 blockers have since been
measured and cleared.** The architecture held every time it was tested and
rollback is the strongest dimension in the system; what was missing was two
measurements, and Phase 3 would otherwise have been coded against guesses about
its own inputs. Those runs cost $0.75, took an afternoon, and changed the
recommended convergence design — which is the strongest argument available for
taking them before writing the loop rather than during.

**Remaining before Phase 3 is enabled** (not before it is written): P1-1, three
real-PR runs for a false-positive rate that is not N=1, and P1-2, a kill switch.

---

## The shape of the problem

Three things went wrong today, and they rhyme.

1. A fence parser copied from an agent that never writes code, into an agent
   whose entire job is code.
2. A `setup-node` cache config copied from a single-package assumption, into a
   monorepo with no root lockfile.
3. A findings JSON replayed against a branch it was never observed on.

Each was a correct decision **carried across a boundary where its premise no
longer held**. None was caught by a test, because tests check what you thought
to check. The items below are mostly instances of the same shape.

---

## P0 — blocks WRITING Phase 3

Both are measurements. Together they cost about $0.54 and an afternoon, and
until they exist, the convergence design is a guess.

### P0-1. ~~The S-1 fix is deployed and has never been run~~ — MEASURED 2026-09-01

**Done. Three corpus runs on `qa-corpus-1` against runtime v2.**

| run | turns | s/turn | routes | S-1 | S-2 | S-3 | cost |
|---|---|---|---|---|---|---|---|
| 33416330680 | 22 | 6.8 | 7 | ✅ | ❌ | ❌ | $0.22 |
| 33416898732 | 24 | 7.0 | 8 | ✅ | ✅ | ❌ | $0.24 |
| 33417399893 | 27 | 7.3 | 7 | ✅ | ✅ | ❌ | $0.28 |

**S-1: 3/3.** The hypothesis held. The click-triggered crash that thirteen
earlier runs never provoked is now caught every time, and in run 33417399893 it
arrives through the deterministic path — `cand-2 confirmed`, a real console error
raised by the click the interaction pass performed. Recall moved from 1/3 to a
mean of 5/9 across three runs.

**S-3: 0/3.** Unchanged, and now measured rather than assumed. It remains the
model-driven member of the set with no deterministic shape.

**S-2: 2/3, and this is the important result** — see the convergence section.
The detector fired in all three runs with a near-identical signal (12, 13, 12
svg-adjacent empty slots on `/maintenance`). The model **refuted** it once and
**confirmed** it twice. The mechanical layer was stable; the verdict on it was
not.

**Still untested:** the third claim in the `EVIDENCE.md` hypothesis — no new
false positives on healthy `main`. These were corpus runs. Supporting but not
conclusive: across all three, every non-seed candidate was correctly refuted,
with no spurious findings.

### P0-2. ~~The turn budget rose 48% and the wall clock was never re-checked~~ — RESOLVED

**Measured, and the worry does not survive contact.**

Pace came in at **6.8, 7.0 and 7.3 s/turn** — faster than the 7.6 the concern was
built on, not slower. The interaction pass costs less per turn than plain reads
did, most likely because prompt caching improves as the session lengthens.

```
turn cap at 8 routes: 62      deadline: 600s
  worst observed 7.3s/turn -> 453s at the cap     comfortable
  breach would need 9.7s/turn                     33% above worst observed
```

Runs used **22, 24 and 27 turns of 62**. The cap is nowhere near binding, so no
run was truncated and the "truncation reads as progress" path into Phase 3 is
currently closed.

**The residual is the opposite of what was feared:** `_TURNS_PER_ROUTE` (3.5) plus
`_INTERACTION_TURNS_PER_ROUTE` (2.0) budgets 5.5 turns per route against roughly
2.3 observed. The ceiling is about 2.4x what the work needs. That is safe but
loose — a stop-loss set that far above reality stops very little. Worth pulling
down once there is a fourth and fifth data point, not now.

## P1 — blocks ENABLING Phase 3, not writing it

### P1-1. Criterion 4 is N=1

`EVIDENCE.md` says so itself: *"Criterion 4 needs the three real PRs before"*.
One healthy-`main` negative check is a data point, not a false-positive rate,
and `PLAN.md`'s own corrected note explains why only real PRs can supply one.

- **Fix:** label three real vesselAI PRs `agent-qa` and record all three.
- **Cost:** ~$0.55.

### P1-2. Phase 3 has no kill switch

Every other spending path has one: `fix_agent_enabled` destroys the fix role at
the account, `QA_SCHEDULE_ENABLED` gates the sweep, the `agent-qa` label gates
PR runs. `PLAN.md` calls for a separate label for Phase 3 and it does not exist.

Phase 3 multiplies spend by round count, which makes it the one phase where a
missing switch costs the most.

- **Fix:** build the label gate in the same commit as the loop, not after.

---

## P2 — real, not blocking

### P2-1. Nothing scans the agent's dependencies, and the one audit that exists cannot fail

The runtime zip ships 20 packages — `bedrock-agentcore`, `pydantic`, `boto3`,
`starlette`, `uvicorn`, `h11`, `urllib3` — 40 MB unpacked, executing in AWS. No
`pip-audit`, no Dependabot, no Renovate. Trivy scans the security-gate *image*;
Checkov scans IaC. Neither looks at the agent.

The only dependency audit in the repo is `ci.yml`:

```yaml
run: npm audit --omit=dev --audit-level=high --prefix app || true
```

`|| true`. It covers `app/`, not the agent, and it cannot fail. `CONTRIBUTING.md`
says **"Gates never silently pass."** This one does, by construction, and it is
the only gate in the repo that does.

Compounding: every requirement is floor-pinned (`>=`) with no lockfile, so
`package-qa-agent.sh` resolves different versions on different days with no
record. The S3 version id pins the *artifact*; nothing pins the *inputs*, so the
build is not reproducible even though the deploy is.

- **Fix:** drop `|| true`; add `pip-audit` over `agents/qa/agent/requirements.txt`
  and the gate requirements; emit a resolved-version manifest into the zip so
  what shipped is recoverable.

### P2-2. CI lints Terraform, IaC, Python and the app — but not the workflows

No `actionlint`, no `shellcheck`. The `setup-node` cache failure that killed a
production run was a workflow-config error, and the shell inside these workflows
now carries real logic — `set -euo pipefail`, arg arrays, output plumbing.

- **Fix:** add both to `ci.yml`, over `.github/workflows/**` and `scripts/**`.

### P2-3. A leftover diagnostic is still shipping

`LOG_PAGE_STATE = "1"`, with the comment *"remove after the blindness is
diagnosed"*. It was diagnosed two passes ago — the candidate layer is what came
out of it. Every navigation still writes a full page-state dump to CloudWatch.

### P2-4. `EVIDENCE.md` understates Phase 1

Criterion 1 is marked PARTIAL for two reasons that no longer hold: the workflow
does pass `--runner-minutes`, and the default model prices at $0.07 rather than
rendering `unpriced`. Phase 1 is 5/6, recorded as 4/6.

### P2-5. A live credential is written to `$GITHUB_ENV`

`ui-qa-agent.yml` puts the real `ANTHROPIC_API_KEY` into the environment of every
subsequent step on the attended path. GitHub masks secrets in logs, so this is
low severity — but it is a pattern GitHub's own hardening guidance says to avoid,
and `PLAN.md` calls this key "a live, server-side, billable credential".

- **Fix:** scope it to the one step that needs it.

### P2-6. Repinning is a standing trap

Every PipelineGuard merge touching `agents/fix/` needs a follow-up PR in vesselAI
plus 11 checks. **Four repins today**, and twice the stale pin looked harmless
and was not — the code was merged and the behaviour was not live.

The pin is correct; not auto-promoting code into another repo's write path is the
whole point. But the toil is now load-bearing on someone remembering.

- **Options:** move a `harness-v1` tag as part of shipping, or have PipelineGuard
  open the repin PR on merge. Worth deciding when Phase 3 forces the question,
  not before.

---

## Convergence — the design decision Phase 3 rests on

`PLAN.md` Phase 3 continues while the finding set shrinks and stalls when it does
not. **That check is unsafe for three independent reasons**, and only the first
is written down.

1. **Run-to-run variance.** `EVIDENCE.md`: S-1 was caught in five runs and missed
   in the sixth; route coverage varied between seven and eight on identical code.
   A set that shrinks between rounds may be noise.
2. **Truncation reads as progress.** P0-2 above. A deadline-killed run reports
   fewer findings, which is indistinguishable from having fixed some.
3. **A patch can remove the symptom without fixing anything.** Demonstrated
   today: the agent changed a line so the reported text no longer appeared, and
   the "fix" was a regression. Re-run QA and that finding is gone — which the
   loop would score as success.

Repeated runs address (1) partly and neither (2) nor (3). A tolerance band
addresses (1) partly, needs a number nobody has, and misses (2) and (3).

### Recommended: converge on candidate DETECTIONS — not findings, not assessments

**[corrected by measurement, 2026-09-01]** An earlier draft of this section said
"anchor on the deterministic candidate layer". The three corpus runs falsified
that as written, and the correction is the most useful thing they produced.

`cand-4` — the `/maintenance` blank health rings, which is S-2 — behaved like
this across three runs against identical code:

| run | detection | count | model verdict |
|---|---|---|---|
| 33416330680 | fired | 12 | **refuted** |
| 33416898732 | fired | 13 | confirmed |
| 33417399893 | fired | 12 | confirmed |

**The detection is stable. The assessment is not.** `candidates.py` computes the
signal mechanically and produced it every time; what varied was the model's
confirm-or-refute verdict on an essentially identical input. So "the candidate
layer is deterministic" is true of only half of it, and the half that reaches
the findings list is the model-driven half.

That distinction is the whole design:

- **Converge on the raw DETECTION set** — `(type, url, count)` tuples straight out
  of `candidates.py`, before any model touches them. Twelve blank rings on
  `/maintenance` is a fact about the page. A fix that removes them takes the
  count to zero, mechanically and checkably. No model opinion is involved, so
  none of the three failure modes above can move it.
- **Do not converge on findings**, which inherit model variance twice over —
  once in noticing, once in assessing.
- **Do not converge on assessments**, which is what the earlier draft implied
  and which the table above rules out.
- **Corroborate with patches.** Count a finding resolved only when an edit
  carrying its `finding_id` actually applied. The harness already records this.
  A finding that vanishes with no corresponding patch is unexplained, and
  unexplained is not progress. This is what closes failure mode (3).

**The trade-off, stated plainly.** Convergence then runs on a narrow signal:
`repeated_svg_empty`, `console_error`, `failed_request`. S-3-class defects — a
wrong colour or status rendering a plausible number — have no deterministic
shape, would be reported but never counted, and at 0/3 they are currently
invisible to the loop either way. That is the correct trade. Those are exactly
the findings that vary run to run, and letting them drive a loop is how the loop
starts lying about its own progress.

**What the variance number turned out to be.** Recall over three runs was 1/3,
2/3, 2/3 — one seed flipping, from one model verdict on one stable signal. That
is small enough that a tolerance band would have been set to ±1 finding, which
is indistinguishable from "ignore the signal". It is also large enough that a
single run cannot be trusted, which is what `EVIDENCE.md` said before any of this
was measured.

Neither of the two options originally on the table survives that: repeated runs
would triple the cost to average out a variance that lives in the *assessment*
step, which detections bypass entirely; a tolerance band would need a number
that turns out to be the same size as the signal.

**Addendum (2026-09-02): the audit's allowed option was built, and six live runs
measured it.** P1.3 chose the NO-GO's permitted fix — repeated runs per round —
rather than this section's recommendation. What was built is
`convergence.aggregate_findings` + `aggregate.py`: QA runs K times per round and
the findings fold by strict majority (≥2 of 3), graded at the most severe of
their reporters, on a prose-tolerant identity (page + Dice over significant
tokens).

The live runs validated this section's warning harder than expected, and each
also exposed a bug in the stopping rule or its ledger. The first run
(33586824290) found the variance is one layer deeper than this section measured:
not just whether a defect is detected, but the summary a model writes for the
same defect is fresh prose each run, so the exact-summary identity key dropped
every finding and the round PASSed on a dirty corpus (fix: pipelineguard#56,
prose-tolerant identity). The second (33588304974) showed the between-round
comparison carried the same prose blindness and read a rephrased-but-persistent
blocker as a new one (fix: #57). The third (33590568978) showed the decision
fired on a round's own pre-fix findings, ending the loop one round before the
correct patch could be verified (fix: #59 — STALL and REGRESSED fire only when a
round applied no patch). The fourth (33593197606) stopped honestly at its round
cap, but its reconciliation ledger called the final round's unmeasured patch
"ineffective" (fix: #60 — a final-round patch is "unverified", not
"ineffective"). The fifth (33596263397) ran the fixed ledger and told the truth.
The sixth (33599986790) PASSed.

One consequence this section should record for whoever inherits it: the strict
majority trades exactly the way this warning says, and the warning now has a run
attached. Run 33599986790's final round had each of its three QA runs report a
*different* 1-of-3 finding — including a CRITICAL `/voyage` SAVINGS anomaly that
two earlier runs had also reported at CRITICAL — and the aggregate dropped them
all. The vote absorbed the variance (a single-run loop would have passed or
failed on a coin flip) and the PASS is truthful *for the majority-reported
blockers*; it is not a clean-corpus certificate. Detections-convergence remains
the better long answer — it bypasses the model-prose step entirely — but it is
the larger change (the findings JSON has no raw detection list). Repeated-runs
majority aggregation is the smaller change the audit allowed, now demonstrated
live.

## What this document is not

It is not a list of everything wrong. The parts that held are not listed here,
and there are more of them: OIDC with no stored keys, three roles each minimal, a
fork guard the IAM trust policy cannot express, an allow-list applied at
selection as well as application, an agent that cannot create files, and a
rollback story where every spending path has a switch.

It is also not a verdict on Phase 2, which works. Its exit criteria were met by
evidence — the agent opened a PR, eleven checks ran on it unassisted, the patch
merged. Everything above is about what the next phase would inherit.

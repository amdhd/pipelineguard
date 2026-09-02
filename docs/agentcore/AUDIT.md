# Phase 1–3 audit — work items and priorities

Audit of `docs/agentcore/PLAN.md` Phases 1–3 against the implementation in
`infra/modules/qa_agent/`, `agents/qa/`, `agents/fix/`, `agents/converge/`, and
the recorded evidence in this directory. Verified against the code; the full
suite passes (487 test functions, 538 passing at audit time; 546 after P0-1).

---

## Verdict: **CAUTION**

Green for continuing to run the pipeline in its current **semi-supervised** mode
— it never auto-merges, a human reviews, and the gates are conservative. Not yet
green to:

- call Phase 1 or Phase 3 **done** per its own exit criteria — Phase 2's
  demonstration criterion is met by referenced evidence (**P1.4 DONE**), or
- let the agent's PR comment gate a merge unsupervised, or
- run Phase 3 (convergence) rounds at scale without a live demonstration.

The system is well-built: measured costs run 3–10× under the derived ceilings,
the fix-scope security model is genuinely good, and the two historical bugs
(staleness replay, fence extraction) were found, pinned as fixtures, and fixed.
But the plan's own exit criteria are not met, and the evidence shows the
self-assessment has flip-flopped under pressure (S-3 went "strong seed" →
"model variance" → "structurally unobservable, rescored 5/6" → "all three claims
false, restored 5/9" — see `EVIDENCE.md`).

---

## 1. Dependencies

**Clean, with three reproducibility/drift caveats — no known vulnerable versions
in the runtime path.**

| Dep | Where | Verdict |
|---|---|---|
| `bedrock-agentcore==1.22.0` | `agents/qa/agent/requirements.txt` | Pinned (P2.4) at 1.22.0, still the latest release (2026-08-18). Two yanked versions (1.4.8, 1.5.0) sit below the pin. Vendored into the deployment zip at package time → the **zip is the lock**; the running runtime cannot drift. |
| `websocket-client==1.9.2` | same file | Pinned (P2.4) at the version the deployed zip was found to contain. No current CVE. 82 KB pure-Python CDP client replacing a 134 MB non-manylinux Playwright wheel. |
| `boto3` | deliberately absent | Runtime-provided; `bedrock-agentcore` pulls its own. Minor skew surface between the vendored zip and the runtime. Low risk. |
| fix/converge deps | **no `requirements.txt`** | Harnesses declare nothing; their only hard dep (boto3) is installed by the `vesselAI` workflow, which this repo cannot see. An unverifiable contract. |
| `express ^4.18.2` (demo app) | `app/package.json` | `^` resolves to patched 4.20.x. But CI's `npm audit --audit-level=high ... \|\| true` is **informational only**. |

**Caveats:** (a) **PARTIALLY RESOLVED** — this caveat had two halves and only
one is closed. ~~Unpinned floors~~ — **pinned** (P2.4, 2026-09-02):
`bedrock-agentcore==1.22.0`, `websocket-client==1.9.2`, read out of the deployed
zip rather than off PyPI, guarded by `test_requirements_are_pinned_exactly`,
deployed as runtime v4. **No hash lock → rebuilds are still not byte-reproducible**,
and that is measured, not assumed: the two zips built two days apart for P2.4
differ in `boto3`/`botocore` (1.43.83 → 1.43.86) because transitives resolve
freely. What fixes a closure today is the uploaded zip, whose S3 version id
`dev.tfvars` pins — an artifact lock, not a source lock, so the zip cannot be
reconstructed from the commit. Tracked as **P2.5**; (b) ~~the `aarch64`
vendoring target is unconfirmed~~ — **resolved**: confirmed by `DISCOVERY.md`
§13 and every corpus run since (see P0-2).

## 2. Interface — exact types, error states, and 5 edge cases

Three programs, CLIs over JSON, structured error envelopes — the design
discipline is strong; the crash surfaces are where the edges are.

- **QA agent** (`agent.py invoke`): returns dicts, never raises — `{"error":
  "unauthenticated"}`, `{"error": "schema_violation", ...}`; harness side
  `{"error": "runtime_unavailable"}` / `{"error": "invalid_runtime_response"}`
  (`harness/main.py`). Findings schema (`schema.py`): `overall`, `pages_tested`,
  `findings[]` with `id/severity/page/summary/evidence/steps_to_reproduce/
  expected/actual`; optional `suspected_source` (nullable), `screenshot`,
  `candidate_assessments`.
- **Fix harness** (`harness.py run`): findings JSON in, mutated tree +
  `{applied[], skipped[], excluded[], errors[], budget}` out. `staleness()`
  returns `str | None`. Exit codes: 0 = anything applied, 1 = nothing applied
  AND something broke (`summary.py`).
- **Converge session** (`session.py`): append-only state JSON, budgets re-read
  from flags every round, `decision/stop/round` to `$GITHUB_OUTPUT`, exit
  0 = PASS/CONTINUE, 1 = non-PASS stop, 2 = program failure.

**Five edge cases:**

1. **Malformed findings JSON crashed the fix harness uncaught** — `json.loads`
   with no guard; a corrupt artefact produced a traceback, not a summary.
   **FIXED in P0-1** (`FindingsLoadError` → rendered summary, exit 1). Same
   guard added to the converge loader (`StateError` → exit 2), including valid
   JSON that is not a JSON object, which previously crashed in `round_record`.
2. ~~**Default `session_id` label is not unique**~~ — **resolved in P1-5**: the
   harness default is now `new_session_id("qa-run")`, unique per invocation; the
   agent's own no-session_id fallback is uuid-based. Two concurrent runs no
   longer collide on keys.
3. ~~**`TARGET_ARCH=aarch64` is unconfirmed**~~ — **resolved**: a packaged
   `aarch64` zip loaded and ran on the managed runtime (`DISCOVERY.md` §13); the
   arch is empirically confirmed, not guessed.
4. ~~**`staleness()` goes silent on a non-git checkout**~~ — **resolved in
   P1-6**: `run()` warns "not a git repository; staleness could not be checked"
   when the checkout has no HEAD and the findings carry a provenance stamp.
   `staleness()` itself still returns `None` (a directory stays a usable replay
   target); the silence is what was wrong.
5. **`LOG_PAGE_STATE = "1"` is still live** in the runtime's
   `environment_variables`, emitting up to ~20k chars per read to CloudWatch,
   with a comment saying "remove after the blindness is diagnosed." → **P0-3**.

Footnotes: `is_authenticated` treats an empty-token logged-in session as
unauthenticated and discards findings; `navigate()` consumes the route budget
before the navigation succeeds.

## 3. Performance — estimated time/memory vs. thresholds

**Nothing breaches a hard threshold. Every resource is bounded by a backstop,
and measured runs sit 3–10× below the ceilings. Memory is a non-issue.**

| Meter | Ceiling | Measured | Backstop |
|---|---|---|---|
| QA turns | 62 (8 routes) | 18 turns (corpus run 33140269258) | `DEFAULT_DEADLINE_SECONDS=600` |
| QA tokens | 3.34 M derived | ~90k–400k | deadline + `token_budget` |
| QA wall-clock | ~470s at 7.6s/turn | 137s | 600s deadline |
| QA cost | — | **$0.03–0.18/run** (vs ~$0.23 estimate) | token/deadline |
| Fix per run | 5 findings, 200k tokens | 29,402 tokens/finding; **$0.09–0.11/finding** | budget, `--max-findings` |
| Loop | 3 rounds, 2 M tokens, 3600s wall-clock, 1500s/round | live 33599986790: 4 rounds, 4.7 M tokens, 2415s wall — **PASS** | cumulative caps from flags |
| Idle runtime | — | ~$1.40/month | — |

**Flagged:** harness `read_timeout=900` exceeds the agent's 600s deadline — a
single hung call can burn 900s, longer than the whole run's budget and 60% of
the per-round cap. The loop degrades safely, but it is a sizing mismatch. →
**P2-2**.

The quadratic token model (`_BASE_TOKENS·n + _TOKENS_PER_TURN·n·(n−1)/2`) is the
standout: it fixed a real bug (a flat 200k budget fired mid-sweep) and ties
budget to route count.

## 4. Security

**Strong — the hard problems (secrets, lateral write scope, untrusted-model
input) are solved the right way. The fork guard, the first of the two out-of-repo
trust dependencies, is now audited (P2.3); one remains.**

- **Secrets never touch Terraform.** Seeded out-of-band via `seed-qa-secret.sh`
  (0600 temp file); the code is aware `terraform show -json` does not redact.
  The dummy AI key default for tunnel runs is good containment.
- **Fix scope is a hard allow-list, not a model promise.** `paths.py` allow-list
  + deny-list + traversal normalization; `edits.py` re-checks resolved symlinks
  against the allow-list after the fact. `prompt.py` treats finding text as
  hostile input (data-block delimiting, backtick/control-char stripping,
  `_FENCE_DELIMITER` anti-escape). The right architecture for a model that
  writes code.
- **OIDC uses `StringEquals` enumerated subjects**, never `StringLike`. The
  `github_fix` role is ref-only (no `pull_request` subject).
- **The fork guard is audited and correct (P2.3, 2026-09-02).** `github_qa`
  still cannot distinguish fork PRs — GitHub mints `sub =
  repo:amdhd/vesselAI:pull_request` for fork and same-repo PRs alike — so the
  control remains workflow-level. Read at `ui-qa-agent.yml` @ `a4784517`, the
  job-level `if` is:

  ```yaml
  (github.event_name != 'pull_request' ||
    (github.event.pull_request.head.repo.fork == false &&
     contains(github.event.pull_request.labels.*.name, 'agent-qa'))) &&
  (github.event_name != 'schedule' || vars.QA_SCHEDULE_ENABLED == 'true')
  ```

  The fork check is **AND**-ed with the label, not offered as an alternative to
  it, so the collapse this audit feared — a maintainer labelling a fork PR to
  review it — does not open the gate. `qa` is the workflow's only job and the
  `if` is job-level, so **Configure AWS credentials** is inside the guard: a fork
  PR reaches no AWS-touching step. `workflow_dispatch` needs write access;
  `schedule` is separately gated on `QA_SCHEDULE_ENABLED`.
- **One out-of-repo trust dependency remains:** the fix harness runs in a GitHub
  runner whose image/install is defined by that same external workflow.

## 5. Tests — what's covered and the minimal missing set

Coverage is unusually honest and deep (487 functions / 538 passing at audit
time). Standouts: the staleness guard is regression-pinned against the real
fixture that caused the "confident, compiling, wrong patches" incident; the
fence-extraction fix is pinned against run 33408195295; path security is
exercised end-to-end; the convergence stopping rule is exhaustively unit-tested.
CI runs all six test dirs, AWS-free.

**Minimal test cases still needed** (each is a real gap, not polish):

1. ~~Malformed findings JSON → clean summary, no traceback~~ **DONE in P0-1**
   (fix harness + converge loader; both files +8 tests).
2. ~~`staleness()` with a non-git checkout → explicit "cannot check" warning~~.
   **DONE in P1-6** (+1 test).
3. ~~Empty-token auth probe → named outcome, not a silent findings discard~~.
   **DONE in P2-1**: an empty `auth_token_key` now records
   `auth_probe: "not_configured"` (distinct from `"measured"`) and the report
   prints an "Auth was not verified" caveat above the findings; +5 tests.
4. `LOG_PAGE_STATE` defaults off / gated. → **P0-3**.
5. ~~Concurrent-runs key isolation under `screenshots/qa-run/`.~~ **DONE in
   P1-5** (default label unique per invocation; +4 tests).
6. ~~**Convergence has no live multi-round test** — `test_convergence.py`'s own
   docstring says the decision surface is exercised with constructed rounds "for
   the price of a unit test."~~ **DONE in P1-3**: six live runs exercised
   QA→fix→decision end-to-end on `qa-corpus-1` (33586824290 → 33599986790, the
   last a PASS), and repeated runs per round were added
   (`convergence.aggregate_findings` + `aggregate.py`: strict-majority vote on a
   prose-tolerant identity). Each early run exposed one stopping-rule or ledger
   bug the fixes below closed. See EVIDENCE.md, Phase 3 section.

## 6. Rollback — feature flags and migration path

**Every phase has a working kill switch, and the code is designed so a mid-loop
stop loses nothing.**

- **Phase 2**: `fix_agent_enabled` (default `false`, count-gated) — flip off and
  the fix role/runtime wiring disappears. Currently `true` in `dev.tfvars`. The
  cleanest rollback in the system.
- **Phase 1**: `qa_pr_enabled` (default `true`) — the account-level kill switch
  for the whole QA-on-PR pipeline, ~~previously a gap~~ **closed by P3-4**.
  PR-QA needs two halves: the `pull_request` trigger in vesselAI's workflow, and
  the `pull_request` subject in `github_qa`'s trust policy. Only the second is
  ours, and dropping it is sufficient — the workflow keeps firing and its
  assume-role fails. Corpus/schedule dispatch is untouched (the `ref:` subjects
  stay), so `qa_corpus_refs` (default `[]`) and the schedule trigger keep
  working, as does the `qa_github_role_arn` output `layer2_ephemeral` reads. The
  PR-comment behavior is gated by exit codes and the marker; the agent never
  auto-merges.
- **Code migration**: `qa_agent_code_key` + `qa_agent_code_version_id` are
  committed and pinned; rollback of an agent build is repointing the version_id
  (an unset key once destroyed the runtime — now prevented by committed
  defaults).
- **State**: convergence state is append-only; budgets re-read from flags; a
  stopped loop resumes without corruption. Destroy-hang already worked around
  via `network_mode = "PUBLIC"`.
- Provider pinned `>=6.18, <7.0`.

---

## Blockers by phase

**Phase 1 — CAUTION.**
1. **Exit criterion 5 (false-negative rate) failing: 5/9**, and S-3 is **0/16** —
   the system silently misses a whole class of defect. Acceptable for an
   advisor; not acceptable to quote "recall" as verified. → **P1-1**.
2. **Exit criterion 1 (runner minutes) unimplemented** — the workflow never
   passes `--runner-minutes`, so the cost table renders `unpriced`. → **P1-2**.
3. **False-positive rate is unproven** — "0 FP on one labelled finding" is not a
   rate; the plan requires 3 real human-labelled PRs. → **P1-7**.
4. ~~**`TARGET_ARCH=aarch64` unverified**~~ — **resolved**: the audit missed
   that `DISCOVERY.md` §13 already confirmed aarch64 empirically (see P0-2).

**Phase 2 — CAUTION, demonstration met.** Well-tested; the staleness guard closes
the known real incident; **one agent PR has merged CI-green** —
[amdhd/vesselAI#102](https://github.com/amdhd/vesselAI/pull/102) (run
`33409654638`, 11 checks green, 2026-08-31) — the P1.4 criterion is now
referenced, not claimed (EVIDENCE.md, Phase 2 section). Remaining: **P2.3**
(fork-PR guard) and **P2.4** (version pinning).

**Phase 3 — demonstration met.** The stopping rule ran live on `qa-corpus-1`
with repeated runs per round: QA K=3 times, aggregated by strict majority on a
prose-tolerant identity (page + Dice over significant tokens), so the
round-to-round comparison is no longer at the mercy of run-to-run variance
(EVIDENCE.md's flagged risk). Six runs (33586824290 → 33599986790) each exposed
and the subsequent fix closed one mechanism bug: an exact-summary aggregation
key produced a false PASS (→ pipelineguard#56); an exact-fingerprint
between-round comparison read a rephrased blocker as new (→ #57); the decision
fired on a round's own pre-fix findings (→ #59); and the reconciliation ledger
called an unmeasured final-round patch "ineffective" (→ #60). The final run,
33599986790, ended in the first live convergence **PASS**. Remaining: loop QA in
fallback mode for corpus seeds, the strict majority's documented tolerance, and
detections-convergence as a future refinement (REMEDIATION.md).

---

# Work items, by priority

Severity = impact of the underlying defect/risk if it manifests (or of the gap
being left unproven). Priority = sequencing (cost-of-delay × effort × what it
gates).

| # | Item | Sev | Loc | Effort | Gates |
|---|------|-----|-----|--------|-------|
| **P0.1** | Guard `json.loads` in fix harness + converge loader | **HIGH** | `agents/fix/harness.py`, `agents/converge/session.py` | **DONE** | — |
| **P0.2** | Verify `TARGET_ARCH` against the runtime, or make it an env override | **HIGH** | `scripts/package-qa-agent.sh` | **DONE** | Phase 1 done |
| **P0.3** | Gate `LOG_PAGE_STATE` behind an env flag, default off | LOW | `infra/modules/qa_agent/main.tf` | **DONE** | — |
| **P1.1** | Close the S-3 recall gap (5/9 → ≥8/9, or documented rationale) | **HIGH** | candidate layer (not `rubric.py`) | **DONE — written scope decision (2026-09-02).** Carve-out (PR #49) withdrawn (eleventh pass); candidate layer (PR #50) merged + measured (twelfth pass): detector correct, Leg B clean, but S-3 0/7 — the raw-`OPEN` signal renders only on the `/sire` Findings tab the agent never materializes. Per the written-decision branch of P1.1's criterion, S-3's symptom (a raw enum rendering only behind a non-default tab) is declared OUT OF SCOPE for the browser-driving agent while the runtime harvests only the DOM the model materializes; the miss is documented and owned, and the detector stays live so any future layer that materializes all views closes it — see [EVIDENCE.md](EVIDENCE.md) twelfth pass | Phase 1 done |
| **P1.2** | Wire `--runner-minutes` through the workflow | LOW | `vesselAI` workflow + `report.py` | **DONE** | Phase 1 criterion 1 |
| **P1.3** | Run Phase 3 live, 2–3 real rounds; add tolerance band or repeated rounds | **HIGH** | `agents/converge/` + runner | **DONE** — runs 33586824290→33599986790 (PASS) | Phase 3 demo met |
| **P1.4** | Demonstrate one agent PR merged CI-green | MEDIUM | fix harness + `vesselAI` workflow | **DONE** | amdhd/vesselAI#102, run 33409654638 |
| **P1.5** | Make the default session label unique (concurrency isolation) | MEDIUM | `agents/qa/harness/main.py` | **DONE** | — |
| **P1.6** | `staleness()` warns on a non-git checkout | MEDIUM | `agents/fix/harness.py` | **DONE** | — |
| **P1.7** | Collect 3 human-labelled PRs for a real false-positive rate | MEDIUM | corpus + `score.py` | ongoing | Phase 1 criterion 4 |
| **P2.1** | Handle empty-token auth probe explicitly | LOW/MED | `agents/qa/agent/agent.py` | **DONE** | — |
| **P2.2** | Align harness `read_timeout=900` with agent deadline 600 | LOW/MED | `agents/qa/harness/main.py` | **DONE** | — |
| **P2.3** | Verify the `vesselAI` fork-PR guard is present and correct | MEDIUM | external workflow (audit) | **DONE** — audited 2026-09-02 against `ui-qa-agent.yml` @ `a4784517`; the fork check is **AND**-ed with the label, so the collapse this item feared does not exist | — |
| **P2.4** | Pin exact versions in `requirements.txt` / add a lockfile | LOW/MED | `agents/qa/agent/requirements.txt` | **DONE** — `==1.22.0` / `==1.9.2` (read out of the deployed zip), guarded by a test; rebuilt and deployed (runtime v4) | — |
| **P2.5** | Hash-lock the transitive closure so a zip is reconstructible from a commit | LOW | `scripts/package-qa-agent.sh` | half-day | — |
| **P3.1** | Don't consume route budget on a failed navigation | LOW | `agents/qa/agent/browser_tools.py` | **DONE** | — |
| **P3.2** | Declare fix/converge runtime deps (a `requirements.txt`) | LOW | `agents/fix/`, `agents/converge/` | **DONE** | — |
| **P3.3** | Make `npm audit` a real gate or label it informational | LOW | `.github/workflows/ci.yml` | **DONE** | — |
| **P3.4** | Add a Terraform-level switch to disable the whole QA-on-PR pipeline | LOW/MED | `infra/modules/qa_agent/` | **DONE** — `qa_pr_enabled` (default `true`) gates the `pull_request` subject only; zero-drift proven by a no-diff plan | — |

## Acceptance criteria per item

**P0.1 — DONE.** `FindingsLoadError` in the fix harness (rendered summary, exit
1, budget always reported, no traceback) and `StateError` in the converge
session (exit 2, corrupt state never silently reset to round 1). +8 tests, 546
pass.

**P0.2 — DONE.** `aarch64` was already confirmed, and the audit missed it:
`DISCOVERY.md` §13 (verified 2026-08-26) records a `manylinux2014_aarch64` /
`cp312` zip loading and running on the managed runtime, and every corpus run
since invoked it. Since `package-qa-agent.sh`'s ELF verification rejects any
build whose native objects do not match `TARGET_ARCH`, a successful load proves
the arch. The `TARGET_ARCH` env override already existed; the script's stale
"assumption not confirmed" comment now points at §13.

**P0.3 — DONE.** `LOG_PAGE_STATE` is now behind a `log_page_state` variable
(default `false`); Terraform **omits** the key when disabled rather than setting
`"0"` (which Python truthiness would still treat as on). The agent's gate is
value-aware (`1`/`true`/`yes` only), so a stray `LOG_PAGE_STATE=0` in an env file
no longer silently enables it. +3 tests; takes effect on the next apply.

**P1.1 — recall.** ≥8/9 on the seeded corpus (3/3 each for S-1, S-2, S-3) with
the run recorded, *or* a written decision that S-3's quiet symptom is out of
scope with the miss documented and owned. The ninth-pass flip-flop history means
the record has to be written first, not the claim.

**P1.1 — MEASURED (2026-09-01), still open.** The eleventh-pass measurement is the
record. PR #49's rubric carve-out was the in-repo half of P1.1; on measurement it
failed both legs — Leg A recall stayed 6/9 (S-3 0/3) and Leg B produced 2 findings
on healthy `main` where the baseline was 0 (F-001 `/voyage` "numbers look odd",
F-002 `/sire` "0 GREEN" on vessel-002 where 0 GREEN is correct — both labelled
false positives). The carve-out is withdrawn. The diagnosis moved the lever:
24 candidates across the three Leg A runs, none mentioning `/sire`, `OPEN`, the
badge, or status — S-3's miss is in candidate generation, not the reporting
rubric.

**P1.1 — MEASURED again (2026-09-02, P1.2 twelfth pass), still open.** The
candidate layer (PR #50) is the measured attempt the eleventh pass prescribed, and
it is now measured to the same answer from the other direction. The
`status_case_leak` detector is correct (unit-tested on the exact `OPEN`/`Closed`
DOM shape; its harvest provably live — `repeated_slots` fires every run), Leg B is
clean (0 findings on healthy `main`), and S-1/S-2 hold at 3/3. But S-3 is **0/7**
corpus runs across the merged, fixed, and coverage passes: the leak renders only on
the `/sire` Findings tab, the agent visits `/sire` in every run yet never
materializes that tab, and the mandatory open-every-view rule (PR #52) changed
nothing. The candidate is unreachable — the miss sits at *materialization*, one
step before the harvest.

**P1.1 — CLOSED via written decision (2026-09-02).** The P1.2 measurement is the
final measured attempt under the current architecture, and the human decision
exercises the written-decision branch of the acceptance criterion above:

- **What is scoped out.** A defect whose symptom renders ONLY on a non-default
  view of a route — content a tab, segmented control or sub-nav hides until a
  user opens it — is out of scope for this browser-driving agent as long as the
  runtime harvests only the DOM the model materializes. S-3 is exactly that shape:
  the raw `OPEN`/`closed` leak exists only on the `/sire` Findings tab, and two
  rubric designs (optional interaction pass, then mandatory open-every-view) both
  failed to make the agent open it (0/6 on the fixed detector, 0/7 overall).
- **What remains in scope.** A defect visible on a route's default view, or on a
  view the agent does open, remains fully reportable. S-3's mechanical half — the
  `status_case_leak` detector — stays in the harness: it is correct, unit-tested,
  and will fire the moment any layer (the current model, or a future harness that
  provokes view-switchers itself) materializes the Findings tab.
- **What is owned.** The miss is measured (recall 6/9, S-3 0/7), named (a
  materialization gate, one step before the harvest), and recorded with run IDs
  in the twelfth-pass EVIDENCE entry. It is a known limitation with a named
  reopening condition, not a silent one.

**P1.2 — criterion 1 complete.** The workflow passes a real `--runner-minutes`;
a run renders a non-`unpriced` three-meter cost table.

**P1.3 — DONE.** One real 2–3-round run where each round's QA+fix steps actually
ran, decision history coherent, and repeated runs per round added to
`convergence.py`. Six live runs on `qa-corpus-1` (33586824290 → 33599986790);
the final one, 33599986790, ran four rounds
(CONTINUE → CONTINUE → CONTINUE → **PASS**) with repeated QA runs aggregated by
strict majority (≥2 of 3) on a prose-tolerant identity, and is the first live
convergence PASS. The five earlier runs each exposed a mechanism bug — a false
PASS, two mislabelled REGRESSEDs, a mislabelled ledger patch — which
pipelineguard#56/#57/#59/#60 closed; they are documented in EVIDENCE.md, not
hidden. Repeated runs were the audit's allowed option; detections-convergence
stays a future refinement (REMEDIATION.md).

**P1.4 — DONE.** A real fix-agent PR (compiles, CI green, no human intervention
on the agent's side) exists and is **referenced, not claimed**:
[amdhd/vesselAI#102](https://github.com/amdhd/vesselAI/pull/102) — authored by
`app/pipelineguard-fix-agent`, produced by the current `agent-fix` workflow (run
`33409654638`, `workflow_dispatch` on `main`), patch
`frontend/src/pages/DashboardPage.tsx` +2/−2 (the "Captain Captain" greeting
duplicate), all 11 CI checks SUCCESS, merged 2026-08-31 (merge commit
`5d91f6b3`). The rollup running on the bot's own PR is the evidence that rules
out the `GITHUB_TOKEN` zero-checks failure (PLAN.md:1102-1105). Caveats,
recorded not hidden: input was the committed fixture `findings-33140664097` (a
real finding, frozen for reproducibility — PLAN.md:1115-1132); a human clicked
the final merge (the designed human gate); the second agent run (PR #105) closed
as wrong — the pipeline also needs a human reviewer. Full record in the Phase 2
section of EVIDENCE.md.

**P1.5 — DONE.** The `--session-label` default is now `new_session_id("qa-run")`,
unique per invocation, so concurrent runs cannot overwrite each other's
`screenshots/` and `reports/` keys; an explicit label still wins. The agent's own
no-session_id fallback also lost its second-resolution timestamp collision
(`run-{int(time.time())}` → uuid). +4 tests.

**P1.6 — DONE.** `run()` now logs "not a git repository; staleness could not be
checked against observed commit <sha>" when the checkout has no HEAD and the
findings carry a stamp — the same loudness the unstamped case already had.
`staleness()` still returns `None` so a plain directory stays a usable replay
target; the silent pass is gone. +1 test.

**P1.7 — FP rate is a rate.** ≥3 PRs with human labels scored by `score.py`; the
rate quoted only over labelled findings.

**P2.1 — DONE.** An empty `auth_token_key` is now a named outcome: `_probe_auth`
returns `(None, "not_configured")` (vs `"measured"`), the findings record
`auth_probe`, and `report.render` prints "Auth was not verified" above the
findings — a PASS cannot hide the fact that the run never checked whether the
agent got past the login page. Unknown is deliberately not a hard fail (a public
target with no token-key auth is legitimate); only a measured `False` discards.
+5 tests.

**P2.2 — DONE.** The invoke read timeout is derived from the deadline the caller
actually configured (`_read_timeout`: `min(900, deadline + 60s startup slack)`)
instead of a hardcoded 900. A caller who raised `--deadline-seconds` toward the
ceiling was previously cut off mid-flight — discarding the same report the
timeout exists to protect. The 900 default is drift-checked against
`agent.DEFAULT_DEADLINE_SECONDS`. +6 tests.

**P2.3 — DONE (audit, 2026-09-02).** Read `.github/workflows/ui-qa-agent.yml`
in `amdhd/vesselAI` at `a4784517` (the current head of that file). Verdict:
**present and correct.** The guard is the job-level `if` quoted in Section 4. The
three things that had to be true are:

1. **The fork check is mandatory, not an alternative.** `head.repo.fork == false`
   is `&&`-ed with the `agent-qa` label, so the failure mode this item was opened
   for — "the label gate collapses the moment a maintainer labels a fork PR" — is
   not reachable. Labelling a fork PR satisfies the label clause and still fails
   the fork clause.
2. **It covers the credential step.** `qa` is the only job; the `if` is
   job-level, so **Configure AWS credentials** (`vars.QA_AGENT_ROLE_ARN`) and
   every step after it are inside the guard. There is no unguarded job or step
   that touches AWS.
3. **The other two triggers are gated too.** `workflow_dispatch` requires write
   access on the repo; `schedule` — the one trigger that can bill unattended —
   additionally requires `vars.QA_SCHEDULE_ENABLED == 'true'`.

**Residual, recorded not closed:** the workflow file a `pull_request` run
executes comes from the PR head, so a fork author can edit the guard text in
their own PR. No workflow-level control can survive that; the backstop is
GitHub's fork-PR token policy, and it is in place — `amdhd/vesselAI` is public
and its default workflow permissions are `read`, so a fork PR is never granted
`id-token: write` and `configure-aws-credentials` has no OIDC token to exchange.
That is a platform default rather than a control this project owns, which is
exactly what the workflow's own comment says. A cheap hardening, not required:
`github.event.pull_request.head.repo.full_name == github.repository` compares
strings and avoids GitHub's loose `== false` coercion, which also treats a null
`head.repo` (deleted fork) as passing.

**P2.4 — DONE (2026-09-02).** `bedrock-agentcore>=1.22.0` /
`websocket-client>=1.9.0` became `==1.22.0` / `==1.9.2`. The audit's framing that
"the zip is the lock" was true of the running runtime and false of a rebuild: a
floor resolves to whatever is latest on the day someone re-packages. **It had
already drifted.** The deployed zip (`GYEn9n0EQFFeQjkVsN.b_.MVBydy6foX`, built
2026-08-31) was pulled and read: it carries `websocket_client-1.9.2.dist-info`,
not the 1.9.0 the floor was written against. A minor version reached production
without anyone choosing it and without the repo recording it — which is why the
pin is 1.9.2, read out of that zip, rather than the 1.9.0 this audit had assumed:
pinning is meant to freeze what runs, not to quietly downgrade it. `test_requirements_are_pinned_exactly` asserts `==` on every
requirement line (not the specific versions — the versions are meant to be bumped
deliberately, the pinning is not); 611 pass.

A hash-locked transitive closure was considered and not done: it would have to be
generated for `manylinux2014_aarch64` / `cp312` rather than for a laptop, which
is a change to `package-qa-agent.sh`, not to `requirements.txt`. The uploaded zip
stays the artifact that fixes the closure per deploy, and `dev.tfvars` pins its
S3 version id.

Verified by rebuild: `scripts/package-qa-agent.sh` installed the pinned set for
the target, passed the ELF/`cp312` verification, and uploaded version
`l90BIgjGrwasJ2v_Sr9V0302MzjT0NQk`. Diffing the staged zip's `dist-info` list
against the deployed one shows the two direct deps now identical and **only the
unpinned transitives moved** — `boto3`/`botocore` 1.43.83 → 1.43.86 across two
days. That is the residual caveat measured rather than asserted: the pin fixes
what this repo declares, and the zip's S3 version id is still what fixes
everything below it.

**Deployed 2026-09-02** (pipelineguard#66). `dev.tfvars` advanced to
`l90BIgjGrwasJ2v_Sr9V0302MzjT0NQk` and `scripts/apply-dev.sh` applied it:
`agent_runtime_version` 3 → 4, serving the pinned zip.

Worth recording, because it revises the deploy model this project has been
carrying: a code-version bump is an **update in-place**, not a recreation.
`terraform plan` read `0 to add, 1 to change, 0 to destroy` on
`aws_bedrockagentcore_agent_runtime.qa[0]`, and after the apply
`terraform output qa_runtime_arn` still equalled vesselAI's `QA_RUNTIME_ARN`
(`…runtime/pipelineguard_qa_dev_runtime-lxQgbh3dlW`), so no
`gh variable set` was needed. The ARN rotation in CLAUDE.md rule 5 is the
consequence of the runtime being **destroyed and recreated** — what the
2026-08-30 incident did by dropping `count` to 0 — not of shipping a new zip.
The rule's sequence still holds; its ARN step is conditional, and this apply is
the measurement of when that condition fires.

**P2.5 — the half of caveat (a) P2.4 did not close.** Pinning the direct deps
made the declaration reproducible; the *closure* still is not. Measured on the
P2.4 rebuild: two zips built two days apart from the same commit differ in
`boto3`/`botocore` (1.43.83 → 1.43.86). The S3 version id in `dev.tfvars` locks
the artifact, so what RUNS is always known — but the zip cannot be rebuilt from
the commit, which is what a supply-chain question would actually ask.

Acceptance: a `pip install --require-hashes`-compatible lock generated **for
`manylinux2014_aarch64` / `cp312`**, not for a laptop, plus a rebuild
demonstrating two zips from one commit with identical `dist-info` sets. The work
is in `scripts/package-qa-agent.sh`, not in `requirements.txt` — which is why
P2.4 did not do it. Low priority: the artifact lock already answers "what is
running", and this answers the rarer "can we prove what went into it".

**P3.1 — DONE.** `navigate()` charges a route only after the CDP navigation
succeeds. A failed `Page.navigate` returns the browser error with current page
state and does NOT consume a budget slot — a broken route can no longer refuse
the model a real one. +2 tests.

**P3.2 — DONE.** `agents/fix/requirements.txt` declares `boto3` (this half runs
on the GitHub runner, not the managed runtime); `agents/converge/requirements.txt`
is a comment-only declaration — the convergence session is deliberately pure
stdlib (reads JSON, applies the stopping rule, writes the ledger).

**P3.3 — DONE.** `npm audit --omit=dev --audit-level=high` is now a real gate.
The old `|| true` swept failures under a step that always went green; the tree
currently passes (its only finding is a LOW `body-parser`), so a new HIGH/CRITICAL
prod dependency now blocks the PR.

**P3.4 — DONE (2026-09-02).** `qa_pr_enabled`, a module + layer1 variable
defaulting to `true`, conditions exactly one entry of `github_qa`'s
`token.actions.githubusercontent.com:sub` list: the `pull_request` subject.

Why that and not the role: Phase 1's QA-on-PR pipeline needs the external
workflow's `pull_request` trigger AND this account's `pull_request` subject.
Only the subject is ours, and removing it is enough — the workflow still fires
and `configure-aws-credentials` fails to assume. Count-gating the whole
`github_qa` role would also have killed `workflow_dispatch`/schedule dispatch (a
live mechanism — P1.3's six corpus runs used it) and broken the
`qa_github_role_arn` output `layer2_ephemeral` reads through remote state.

Two plans against real state are the evidence, both plan-only — nothing applied:

- **Default (`true`): `No changes. Your infrastructure matches the configuration.`**
  The `concat` renders today's subject list in today's order, so the switch is
  inert until someone chooses it.
- **`-var='qa_pr_enabled=false'`: `0 to add, 1 to change, 0 to destroy`** — the
  role updated in place, its subject list losing `repo:amdhd/vesselAI:pull_request`
  and keeping `repo:amdhd/vesselAI:ref:refs/heads/main`.

The switch ships **off-by-default-unused**: `dev.tfvars` does not set it, so
PR-QA keeps running until someone adds `qa_pr_enabled = false` and applies.

**P2.x / P3.x** — each gets its named outcome + test.

## Suggested first sprint (all in-repo, all cheap)

1. **P0-1** JSON-parse guards — **DONE** (30 min, +8 tests)
2. **P0-3** gate the diagnostic — **DONE** (15 min, +3 tests)
3. **P0-2** arch verification — **DONE** (already confirmed by DISCOVERY.md §13; script comment corrected)
4. **P1-5** unique session label — **DONE** (1–2 h, +4 tests)
5. **P1-6** staleness warning — **DONE** (30 min, +1 test)

Everything P1 upstream of it — S-3, runner-minutes, the live Phase 3 run, the
labelled-PR sample — is what actually unlocks a phase being signed off as
**done**, and each depends on the runner/`vesselAI` side rather than on this
repo alone.

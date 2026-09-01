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

- call any phase **done** per its own exit criteria, or
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
| `bedrock-agentcore>=1.22.0` | `agents/qa/agent/requirements.txt` | Floor exactly matches latest (1.22.0, 2026-08-18). Two yanked versions (1.4.8, 1.5.0) sit below the floor. Vendored into the deployment zip at package time → the **zip is the lock**; the running runtime cannot drift. |
| `websocket-client>=1.9.0` | same file | No current CVE. 82 KB pure-Python CDP client replacing a 134 MB non-manylinux Playwright wheel. |
| `boto3` | deliberately absent | Runtime-provided; `bedrock-agentcore` pulls its own. Minor skew surface between the vendored zip and the runtime. Low risk. |
| fix/converge deps | **no `requirements.txt`** | Harnesses declare nothing; their only hard dep (boto3) is installed by the `vesselAI` workflow, which this repo cannot see. An unverifiable contract. |
| `express ^4.18.2` (demo app) | `app/package.json` | `^` resolves to patched 4.20.x. But CI's `npm audit --audit-level=high ... \|\| true` is **informational only**. |

**Caveats:** (a) unpinned floors + no hash lock → rebuilds are not reproducible;
(b) ~~the `aarch64` vendoring target is unconfirmed~~ — **resolved**: confirmed
by `DISCOVERY.md` §13 and every corpus run since (see P0-2).

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
| Loop | 3 rounds, 2 M tokens, 3600s wall-clock, 1500s/round | **not yet run live** | cumulative caps from flags |
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
input) are solved the right way. Two trust dependencies live outside this repo.**

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
- **Two out-of-repo trust dependencies:** (1) `github_qa` cannot distinguish
  fork PRs — the guard is a workflow-level fork check in `vesselAI`'s
  `ui-qa-agent.yml`, not auditable from this repo; (2) the fix harness runs in a
  GitHub runner whose image/install is defined by that same external workflow.

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
6. **Convergence has no live multi-round test** — `test_convergence.py`'s own
   docstring says the decision surface is exercised with constructed rounds "for
   the price of a unit test." → **P1-3**.

## 6. Rollback — feature flags and migration path

**Every phase has a working kill switch, and the code is designed so a mid-loop
stop loses nothing.**

- **Phase 2**: `fix_agent_enabled` (default `false`, count-gated) — flip off and
  the fix role/runtime wiring disappears. Currently `true` in `dev.tfvars`. The
  cleanest rollback in the system.
- **Phase 1**: `qa_corpus_refs` (default `[]`) + the schedule trigger. The
  PR-comment behavior is gated by exit codes and the marker; the agent never
  auto-merges. **Gap:** no Terraform-level switch to turn the whole QA-on-PR
  pipeline off — the `pull_request` trigger lives in the external workflow.
  → **P3-4**.
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

**Phase 2 — CAUTION.** Well-tested; the staleness guard closes the known real
incident. But **no agent PR has ever been merged CI-green** — the criterion is
encoded (caps ≤5 files/≤120 lines), not demonstrated. → **P1-4**.

**Phase 3 — NO-GO on unsupervised operation.** The stopping rule is unit-tested
but **never run live**, and `EVIDENCE.md` itself flags the blocking
methodological risk: run-to-run variance is large enough that "a set that
shrinks between two rounds may be noise rather than progress," and the plan
assumes neither repeated runs per round nor a tolerance band. → **P1-3**.

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
| **P1.1** | Close the S-3 recall gap (5/9 → ≥8/9, or documented rationale) | **HIGH** | seeds + `rubric.py` | days | Phase 1 done |
| **P1.2** | Wire `--runner-minutes` through the workflow | LOW | `vesselAI` workflow + `report.py` | 1–2 h | Phase 1 criterion 1 |
| **P1.3** | Run Phase 3 live, 2–3 real rounds; add tolerance band or repeated rounds | **HIGH** | `agents/converge/` + runner | 1–2 days incl. cost | Phase 3 (NO-GO) |
| **P1.4** | Demonstrate one agent PR merged CI-green | MEDIUM | fix harness + `vesselAI` workflow | 1–2 days | Phase 2 done |
| **P1.5** | Make the default session label unique (concurrency isolation) | MEDIUM | `agents/qa/harness/main.py` | **DONE** | — |
| **P1.6** | `staleness()` warns on a non-git checkout | MEDIUM | `agents/fix/harness.py` | **DONE** | — |
| **P1.7** | Collect 3 human-labelled PRs for a real false-positive rate | MEDIUM | corpus + `score.py` | ongoing | Phase 1 criterion 4 |
| **P2.1** | Handle empty-token auth probe explicitly | LOW/MED | `agents/qa/agent/agent.py` | **DONE** | — |
| **P2.2** | Align harness `read_timeout=900` with agent deadline 600 | LOW/MED | `agents/qa/harness/main.py` | 30 min | — |
| **P2.3** | Verify the `vesselAI` fork-PR guard is present and correct | MEDIUM | external workflow (audit) | 1–2 h | — |
| **P2.4** | Pin exact versions in `requirements.txt` / add a lockfile | LOW/MED | `agents/qa/agent/requirements.txt` | 1–2 h | — |
| **P3.1** | Don't consume route budget on a failed navigation | LOW | `agents/qa/agent/browser_tools.py` | 30 min | — |
| **P3.2** | Declare fix/converge runtime deps (a `requirements.txt`) | LOW | `agents/fix/`, `agents/converge/` | 30 min | — |
| **P3.3** | Make `npm audit` a real gate or label it informational | LOW | `.github/workflows/ci.yml` | 15 min | — |
| **P3.4** | Add a Terraform-level switch to disable the whole QA-on-PR pipeline | LOW/MED | `infra/modules/qa_agent/` | half-day | — |

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

**P1.2 — criterion 1 complete.** The workflow passes a real `--runner-minutes`;
a run renders a non-`unpriced` three-meter cost table.

**P1.3 — Phase 3 de-NO-GO.** One real 2–3-round run where each round's QA+fix
steps actually ran, decision history coherent, and either repeated runs per
round or a tolerance band added to `convergence.py`.

**P1.4 — Phase 2 live.** A real fix-agent PR (compiles, CI green, no human
intervention on the agent's side) exists and is referenced, not claimed.

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

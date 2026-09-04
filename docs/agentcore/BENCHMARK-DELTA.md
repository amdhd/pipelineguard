# Benchmark delta — PR-QA loop evolution (goal: "similar or better" than the reference demo)

Reference benchmark: [timwukp/Claude-code-on-AWS-Bedrock-Token-monitoring-alarm-system PR #29](https://github.com/timwukp/Claude-code-on-AWS-Bedrock-Token-monitoring-alarm-system/pull/29) — a live AgentCore UI-QA ↔ Bug-Fix loop, kept open as a showcase. This repo is PipelineGuard (two-layer infra in `amdhd/pipelineguard`; the QA target + workflows live in `amdhd/vesselAI`, out of this repo's edit scope — flagged, not silently edited).

Decision, 2026-09-03: **human-gated + reverify** fix loop (not the demo's auto-push); **document first, then build**.

---

## 1. What "similar or better" means here

The demo shows three things PipelineGuard's PR path does not yet render or wire. Everything else the demo displays, PipelineGuard already does — in most cases more honestly. The job is to close the three real gaps **without regressing** the safety and measurement PipelineGuard already has.

### Already ahead of the benchmark (do not regress)

| Capability | Benchmark (PR #29) | PipelineGuard |
|---|---|---|
| Findings presentation | one-line severity table | per-finding `<details>`: Expected / Actual / Evidence / Steps + inline screenshot + `suspected_source` (`agents/qa/harness/report.py`) |
| Cost honesty | absent | three meters + `unpriced` (never fake $0.00) + prompt-cache hit line (`report.py`, `pricing.py`) |
| FP/FN measurement | prose epilogue ledger | `score.py`: rates over labelled findings only; unlabelled never counted correct |
| Fix safety | **auto-pushes to the PR branch** | fix opens a reviewable PR (`app/pipelineguard-fix-agent`) |
| Loop termination | naive round counter — its own epilogue: "burned its 3 fix rounds" | real stopping rule + reconciliation ledger (`agents/converge/`, P1.3; fixes #56–#60) |
| Kill switches / budgets / security | — | `qa_pr_enabled` (P3.4), fork-guard AND-label, allow-list fix scope, budget backstops |

### The three real gaps

1. **A re-verify block in the QA comment** — the benchmark renders `🔁 Prior findings re-verified | FIXED / STILL_FAILING`. PipelineGuard already owns the hard half (prose-tolerant Dice identity + reconciliation semantics, `agents/converge/aggregate.py`, fixes #56–#60) but only on the corpus converge path, never rendered as a comment ledger on a PR.
2. **A PR-stable prior-report store** — current S3 keys are run-scoped (`gh-<run>[-1]`). Re-verify needs "the last report for *this* PR".
3. **A fix loop that reads as one loop on the PR** — today QA reports and the fix agent opens a detached PR. There is no visible fix → re-test → resolved chain, and no final reconciliation board.

## 2. Target end-state (human-gated, two-repo aware)

Per QA run on a real PR:

1. QA agent explores and posts the findings comment (today's behaviour, unchanged).
2. On blocking findings, the existing fix path opens a **reviewable fix PR** whose body links the origin report.
3. QA on the fix PR **reconciles against the origin report**: each prior finding is classified `FIXED` / `STILL_FAILING` / `REGRESSED` / `UNVERIFIED` (the `UNVERIFIED` case is fix #60's rule — a final-round patch is never called "ineffective" without a re-test). The comment renders that table.
4. A **human** merges the fix PR. The origin PR's next QA re-verifies clean.
5. When the run reaches a clean state, the comment renders a **final reconciliation board**: per-finding ledger (fixed / by-design / agent-noise) — emitted as prose **and** as machine-readable JSON (the demo's prose-only board is the anti-pattern to avoid; `score.py` must be able to consume it).

Round chaining is capped by the human gate itself — no naive auto-push, no unattended round counter (the benchmark's own failure mode).

## 3. Phased plan

| # | Item | Where | Effort | Verify |
|---|---|---|---|---|
| **D-1** | Bank the planted-defect PR as seed S-4 in a real corpus | `agents/qa/harness/corpus.json` | **DONE 2026-09-03** | `score.py` marks S-4 detected on run 33773198501's findings |
| **D-2** | Reconciliation ledger data + `🔁 Prior findings re-verified` renderer | this repo: `report.py` + reuse `aggregate.py` identity | **DONE 2026-09-04** | unit tests: same-finding re-present → STILL_FAILING; absent → FIXED; rephrased → still matched via Dice; final-round untested → UNVERIFIED |
| **D-3** | PR-stable report store (write `pr-<n>/latest` on every PR run) + harness fetches prior report to reconcile | this repo archive step **+ vesselAI workflow flag** | **DONE 2026-09-04** (code + deploy + wiring + live two-run verify) | ✅ two QA runs on PR #125; run 2's comment showed the re-verify table (STILL FAILING ×2, NOT REPRODUCED ×1) |
| **D-4** | Human-gated fix-loop chaining + final reconciliation board (prose + machine-readable ledger) | vesselAI workflows (flag) + this repo renderer | **DONE 2026-09-04** | live on scratch pair demo/d4-origin (vesselAI #129): clean origin run `33855385944` rendered the Final board + wrote `board.json` (1 FIXED via fix-verdict); the two real fixes landed on vesselAI main via PR #134, QA-verified zero findings |
| **D-5** | 3 real human PRs → labelled into `corpus.json` → **P1.7 FP rate** | corpus + vesselAI | ongoing — runway READY 2026-09-04 (runbook: `MEASUREMENT-P1-7.md`); 0/3 real PRs labelled | `score.py` over ≥3 real-PR findings |

The re-verify classifier must NOT reuse `schema.finding_fingerprint` alone for the between-round check: fingerprints exclude `evidence` and renumber per run, so a rephrased blocker must still match (this is exactly the P1.3 bug #57 the aggregate identity already solves — reuse it, do not reinvent).

## 4. Ownership / boundaries

- **In-repo and safe now:** D-2 (renderer + ledger, with tests), the D-3 archive-half. No apply, no infra.
- **vesselAI, flagged:** D-3 workflow wiring, D-4 chaining, D-5 dispatch. Edits land in `amdhd/vesselAI` workflows per the repo rule — never silently.
- **Corpus semantics (unchanged):** seeded PRs (like #125) are FN/detection data (seed-id labels). Only genuinely-human PRs feed the P1.7 FP rate. Mixing them would corrupt the rate `score.py` exists to protect.

## 5. Status

- **D-1 done.** See `agents/qa/harness/corpus.json` (S-4 = enum-vs-display-label count defect, `/maintenance`, source PR #125 / run 33773198501).
- **D-2 done.** `converge.verify_report(prior, current, *, fix_intervened)` in `agents/converge/convergence.py` reuses the Dice identity from `_similarity_clusters` (not `finding_fingerprint` alone) and emits STILL_FAILING / FIXED / NOT_REPRODUCED / UNVERIFIED rows keyed by fingerprint; `report.render_reverify()` + a `reverify_rows=` param on `report.render()` (renders the block above the cost table, and on a run that errored so UNVERIFIED still lands). Tests: `TestVerifyReport` in `agents/converge/tests/test_convergence.py`, `TestReverifyBlock` in `agents/qa/harness/tests/test_report.py`. 202 passed. Wiring the prior report in is D-3.
- **D-3 in-repo code done.** Agent half (`agents/qa/agent/agent.py`): `_archive` also writes `reports/{namespace}/latest/findings.json` when the payload carries `report_namespace` — last write wins, so the alias always points at the most recent attempt, error or not; `_safe_report_namespace` restricts it to one path segment; the alias is best-effort (a failure never loses the run-scoped copy). Harness half (`agents/qa/harness/main.py`): `--reports-bucket` / `--report-namespace` flags; `fetch_prior_report()` reads the alias (404/corrupt → None, reads only); `run()` forwards the namespace in the payload and hands `converge.verify_report(prior, current, fix_intervened=False)` rows to `report.render(... reverify_rows=...)`. Nothing changes when the flags are absent. 639 passed.
- **D-3 ops done 2026-09-04** (deployed + wired end to end): (1) agent zip re-packaged and layer1 applied from main — in-place, `agent_runtime_version` 4 → 5, ARN held, no `QA_RUNTIME_ARN` change (`0b77f10`); (2) `github_qa` role gained a single `s3:GetObject` scoped to `reports/*` — the bucket is CMK-encrypted with the key its `kms:Decrypt` already opened, so no new KMS grant (`926b256`); (3) vesselAI `ui-qa-agent.yml` now passes `--reports-bucket ${{ vars.REPORTS_BUCKET }}` and a PR-only `--report-namespace` (PR #126), with `REPORTS_BUCKET = pipelineguard-qa-dev-reports-149751500899` set as a repo variable.
- **D-3 live verify done 2026-09-04** on vesselAI PR #125 (QA seed `test/qa-seed-equipment-health-summary`). Two namespace-aware runs: run `33782331480` (2 MEDIUM + 1 LOW, incl. the S-4 planted defect) archived `reports/pr-125/latest/findings.json`; run `33782923873` fetched it, re-ran, and rendered the `🔁 Prior findings re-verified` table — the two defects found again showed `STILL FAILING` (Dice identity matched the `/compliance` ETS finding across a MEDIUM→HIGH severity drift), and the one that did not reappear (`/knowledge`) showed `NOT REPRODUCED`, not FIXED, since no fix intervened. Correct per the fix-#60 rule.
  - **Operational finding (2026-09-04):** GitHub Actions sources `pull_request` runs from the PR **head branch's** workflow file, not the base branch's. The first three runs after merging PR #126 executed the pre-merge workflow (invocation lacked the reports flags, no alias written). Fix: merged `main` into the seed branch (only `.github/workflows/ui-qa-agent.yml` changed; the planted defect survived — S-4 still detected). Runs on an old PR will keep using the old workflow until its head carries the change.
- **D-4 done 2026-09-04** — human-gated fix-loop chaining + final reconciliation board, demonstrated live on the scratch pair `demo/d4-origin` (vesselAI PR #129) + `demo/d4-fixbase`, then closed out. What shipped: (1) the fix harness commits a `qa-fix-origin.json` sidecar linking the fix PR to its origin findings and labels the PR `agent-qa`, so QA on the fix PR fires automatically behind the existing label gate; (2) the QA harness reconciles an origin prior against the fix's applied set and writes `reports/pr-<origin>/latest/fix-verdict.json` (status ∈ fixed | still failing | not reproduced | unverified; written only when a prior exists; a run that errors writes rows all `unverified`, never a false PASS); (3) a clean origin run emits the board — prose `🧾 Final reconciliation board` **and** machine `board.json` (schema `pipelineguard/board/v1`), consumable by `score.py --board` (board labels join the corpus; an existing corpus label always wins). In this repo: PR #74 (`7e968d7`, on main). vesselAI workflow edits were PR #128 (flagged per the two-repo rule).
- **D-4 live verify done 2026-09-04** (the loop end-to-end, non-destructive — vesselAI `main` was untouched by the chain): origin run 1 on PR #129 found two REAL mock-app defects — the v3 ETS/MRV cross-view mismatch, and voyage savings stored in a fabricated dollar-scale unit while the frontend contract is tonnes × $650/MT (the savings finding is R8 below). Two fix legs (#130–#132) each opened a fix PR that was auto-QA'd against the origin report; the human gate caught that fix leg #131's first patch hit the wrong layer (R8) and corrected it before merge; the final origin run (`33855385944`, commit `52b9445`) came back clean and rendered the **Final reconciliation board** — 1 row FIXED via fix-verdict attribution, `board.json` written. A third origin finding — a MEDIUM on `/knowledge` — surfaced only on that final run (QA-run variance) and was treated honestly as a new non-blocking finding, not retro-fitted into the board.
- **The two real fixes landed on vesselAI `main`** as PR #134 (with a CII null-guard and the /knowledge label fix), QA-verified zero findings on `main`. Real-fix PRs like this one are the honest *sample* for the FP rate — see D-5 / `MEASUREMENT-P1-7.md` §Why.
  - **Operational finding — R8, wrong-layer auto-fix (2026-09-04):** when a data bug is masked by a surface multiplier, the fix agent can patch the WRONG LAYER. It "fixed" the savings finding by deleting the frontend ×650 (which still showed a positive 'saving' on overruns) instead of fixing the backend mock units. A human gate must verify a patch against the data contract, not just that the surface number shrank. This is exactly why the loop is human-gated rather than auto-merge.
  - **Operational finding — the fix-role trust knob is module-side.** `infra/modules/qa_agent/` ships `fix_workflow_refs`, default `["refs/heads/main"]`, wired into the github_fix trust. The demo widened it to a scratch branch through root-level plumbing (layer1 pass-through + dev.tfvars TEMPORARY block) that was reverted at close-out; live trust re-verified main-only via `aws iam get-role`. To run another scratch-branch fix dispatch, re-add the root knob, apply via `scripts/apply-dev.sh`, then remove + re-apply when done.
  - **Semantic refinement over §2 item 3:** on the PR path there is no REGRESSED verify status — a current finding with no prior counterpart renders as a "New findings not in the origin report" note (and a new HIGH/CRITICAL still exits 1). REGRESSED stays a round-ledger concept only; the PR path never fabricates an origin row.
- **D-5 ready, awaiting humans.** The FP-rate machinery is proven — dry-run 2026-09-04 on the archived pr-129 artifacts showed `score.py` correctly reports `not measured` until a human labels, board labels join the corpus, and one corpus label flips the rate into a figure. Runbook: `MEASUREMENT-P1-7.md`. **0 of 3 real PRs labelled yet** — the missing input is genuinely-human PRs, which only the vesselAI team supplies.

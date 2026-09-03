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
| **D-3** | PR-stable report store (write `pr-<n>/latest` on every PR run) + harness fetches prior report to reconcile | this repo archive step **+ vesselAI workflow flag** | half-day in-repo + vesselAI change | two dispatched QA runs on one PR; second comment shows the re-verify table |
| **D-4** | Human-gated fix-loop chaining + final reconciliation board (prose + machine-readable ledger) | vesselAI workflows (flag) + this repo renderer | larger; coordinated | fix PR QA comment reconciles origin findings; board JSON consumed by `score.py` |
| **D-5** | 3 real human PRs → labelled into `corpus.json` → **P1.7 FP rate** | corpus + vesselAI | ongoing | `score.py` over ≥3 real-PR findings |

The re-verify classifier must NOT reuse `schema.finding_fingerprint` alone for the between-round check: fingerprints exclude `evidence` and renumber per run, so a rephrased blocker must still match (this is exactly the P1.3 bug #57 the aggregate identity already solves — reuse it, do not reinvent).

## 4. Ownership / boundaries

- **In-repo and safe now:** D-2 (renderer + ledger, with tests), the D-3 archive-half. No apply, no infra.
- **vesselAI, flagged:** D-3 workflow wiring, D-4 chaining, D-5 dispatch. Edits land in `amdhd/vesselAI` workflows per the repo rule — never silently.
- **Corpus semantics (unchanged):** seeded PRs (like #125) are FN/detection data (seed-id labels). Only genuinely-human PRs feed the P1.7 FP rate. Mixing them would corrupt the rate `score.py` exists to protect.

## 5. Status

- **D-1 done.** See `agents/qa/harness/corpus.json` (S-4 = enum-vs-display-label count defect, `/maintenance`, source PR #125 / run 33773198501).
- **D-2 done.** `converge.verify_report(prior, current, *, fix_intervened)` in `agents/converge/convergence.py` reuses the Dice identity from `_similarity_clusters` (not `finding_fingerprint` alone) and emits STILL_FAILING / FIXED / NOT_REPRODUCED / UNVERIFIED rows keyed by fingerprint; `report.render_reverify()` + a `reverify_rows=` param on `report.render()` (renders the block above the cost table, and on a run that errored so UNVERIFIED still lands). Tests: `TestVerifyReport` in `agents/converge/tests/test_convergence.py`, `TestReverifyBlock` in `agents/qa/harness/tests/test_report.py`. 202 passed. Wiring the prior report in is D-3.
- D-3 → D-5 pending.

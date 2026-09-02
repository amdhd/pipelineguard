# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## Project — PipelineGuard → AgentCore UI-QA

**Read before touching code, infra, or deploying.** The non-obvious constraints below are the lessons this project paid for; violating one destroys a live resource or burns a paid agent run. `docs/agentcore/PLAN.md` is the phased build spec and `docs/agentcore/EVIDENCE.md` is the measurement record — read both before proposing any change to the QA agent or claiming progress.

### The two-layer AWS layout (read before any terraform command)

Infra is **two separate Terraform roots**, each with its own state in `s3://pipelineguard-tfstate-<acct>-ap-southeast-1/`:

- **`infra/layer1_persistent/`** — the ALWAYS-ON QA core (`aws_kms_key.main` + `module.qa_agent`). Stays up for the vesselAI QA workflow; bills ~**$1.40/mo** idle. State key `pipelineguard/layer1/dev/...`. **Never destroyed for routine teardown.**
- **`infra/layer2_ephemeral/`** — the demo/live stack (networking + ecr + ecs + pipeline + gates). Apply → demo → destroy with `scripts/demo-up.sh` / `scripts/demo-down.sh`. While down it bills ~$0. State key `pipelineguard/layer2/dev/...`. Reads layer1's KMS key via `terraform_remote_state.layer1`.

The QA runtime is PUBLIC-mode (no VPC/ENI/NAT) and independent of layer2 — that is why the demo layer can vanish without touching the QA workflow. Old single-root `infra/` files (main.tf, kms.tf, variables.tf, outputs.tf, versions.tf, environments/) are gone; the shared `infra/modules/` is unchanged.

### Hard rules (do not violate)

1. **Never apply or destroy `layer1_persistent` without `-var-file=dev.tfvars`.** The AgentCore runtime's `count` is `var.qa_agent_code_key == "" ? 0 : 1`. The two code vars are **pinned in `layer1_persistent/dev.tfvars` ON PURPOSE** (the 2026-08-30 incident: a bare apply dropped count to 0 and destroyed a working runtime, and AgentCore regenerates the runtime ARN on recreate — so vesselAI's `QA_RUNTIME_ARN` went stale too). A bare `terraform apply` in that directory is the hazard. Use the wrappers (`scripts/apply-dev.sh` for layer1, `scripts/reopen-corpus.sh` for a targeted corpus re-open) — long `-var=... -target=...` commands split on paste, so run them from script files, never inline one-liners.

2. **`qa_corpus_refs` stays CLI-only** (never in a tfvars file). It opens the GitHub-role trust policy to a corpus branch; open via `scripts/reopen-corpus.sh` (which targets `layer1`), and close (re-apply without the var) when the corpus runs are done. `demo-down.sh` / layer2 ops can never touch this role.

3. **Tests run with the repo venv, not system Python:** `.venv/bin/python -m pytest`. The system `python3.14` has no pytest. Test style: plain functions + classes, `sys.path.insert(0, parents[1])`, substring asserts, detailed WHY docstrings.

4. **After changing agent code, re-package.** `scripts/package-qa-agent.sh` zips `agents/qa/agent/` for a Linux aarch64 managed runtime (cross-platform pip; never assume Mac wheels run). Its file-copy loop must list **every** module (`agent.py rubric.py schema.py browser_tools.py cdp.py candidates.py`) — a missing module is a silent `ImportError` at invoke time, after a browser session is already paid for.

5. **Deploy sequence after an agent change:** package (captures `VERSION_ID`; prints `qa_agent_code_key` / `qa_agent_code_version_id` — the key is the same S3 object every time, so in practice only the version id moves) → commit both values into `layer1_persistent/dev.tfvars` → `scripts/apply-dev.sh` **from the main checkout, not a worktree** (`.terraform/` and `backend.conf` are gitignored, so a worktree dies at backend init) → compare `terraform output qa_runtime_arn` against `gh variable get QA_RUNTIME_ARN --repo amdhd/vesselAI`, and `gh variable set` it **only if they differ** → trigger the workflow.

   **A zip bump does NOT recreate the runtime.** Measured 2026-09-02 (#66): the plan reads `0 to add, 1 to change, 0 to destroy`, `agent_runtime_version` increments (3 → 4), and the ARN holds. AgentCore regenerates the ARN when the runtime is **destroyed and recreated** — which is what the 2026-08-30 incident did by dropping `count` to 0, not what shipping new code does. Expect an in-place update; if a plan on an agent-code change proposes a *replace* or a *destroy*, something else is wrong — stop and read it before applying.

### Architecture (so you don't conflate the two programs)

- **The agent** lives in the AgentCore runtime, packaged as a flat-layout zip from `agents/qa/agent/`. It drives a cloud Chromium browser and emits findings JSON.
- **The harness** runs on the GitHub runner (`agents/qa/harness/`), calls `InvokeAgentRuntime`, enforces budgets, validates JSON, posts the comment. It holds no rubric.
- **Model default is sonnet** (`global.anthropic.claude-sonnet-4-6`); haiku is an explicit `--model` opt-in. `model_profile_ids` in infra and `prices.json` in the harness must stay in sync (the `test_every_granted_rung_is_priced` test guards this).
- **Deterministic candidate layer** (`agents/qa/agent/candidates.py`): the runtime mechanically detects `repeated_svg_empty` / `console_error` / `failed_request` signals; the model **must** assess every candidate (`confirmed` → finding with `finding_id`, or `refuted` → one-line reason) in `candidate_assessments`. This contract is load-bearing in the schema, rubric, and agent tests. Never weaken it.
- **Reports** land in S3: `s3://pipelineguard-qa-dev-reports-<account>/reports/gh-<run>[-1]/findings.json` (a `-1` suffix appears on some runs).

### CloudWatch gotcha

QA runtime log timestamps are **+8h ahead of local**; GitHub workflow timestamps are UTC. Build CloudWatch query windows from the gh epoch values verbatim — do not convert them to local time before querying.

### Repository conventions

- Commit style: `type(scope): summary` (e.g. `feat(agent): …`, `docs(agentcore): …`), ending with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Commit/push only when the user asks. If on `main`, branch first.
- The QA target lives in a separate repo (`amdhd/vesselAI`); the workflow there is out of this repo's scope to edit — flag, don't silently edit.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

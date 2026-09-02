# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## Project — PipelineGuard → AgentCore UI-QA

**Read before touching code, infra, or deploying.** The non-obvious constraints below are the lessons this project paid for; violating one destroys a live resource or burns a paid agent run. `docs/agentcore/PLAN.md` is the phased build spec and `docs/agentcore/EVIDENCE.md` is the measurement record — read both before proposing any change to the QA agent or claiming progress.

### Hard rules (do not violate)

1. **Never run a full `terraform apply` without the code vars.** The AgentCore runtime's `count` is `var.qa_agent_code_key == "" ? 0 : 1` — a bare apply (no `qa_agent_code_key` / `qa_agent_code_version_id`) destroys the runtime (1 → 0). These vars are **CLI-only, never in `dev.tfvars`**. For any apply that touches `module.qa_agent`, use a targeted helper script (the `scripts/reopen-corpus.sh` pattern: `-var-file=environments/dev.tfvars -auto-approve -target=...`). Long `-var=... -target=...` commands split on paste — always run them from a script file, not an inline one-liner.

2. **`qa_agent_code_key`, `qa_agent_code_version_id`, and `qa_corpus_refs` are CLI-only.** Never add them to `infra/environments/dev.tfvars`. `qa_corpus_refs` opens the GitHub-role trust policy to a corpus branch; open via `scripts/reopen-corpus.sh`, and close (re-apply without the var) when the corpus runs are done.

3. **Tests run with the repo venv, not system Python:** `.venv/bin/python -m pytest`. The system `python3.14` has no pytest. Test style: plain functions + classes, `sys.path.insert(0, parents[1])`, substring asserts, detailed WHY docstrings.

4. **After changing agent code, re-package.** `scripts/package-qa-agent.sh` zips `agents/qa/agent/` for a Linux aarch64 managed runtime (cross-platform pip; never assume Mac wheels run). Its file-copy loop must list **every** module (`agent.py rubric.py schema.py browser_tools.py cdp.py candidates.py`) — a missing module is a silent `ImportError` at invoke time, after a browser session is already paid for.

5. **Deploy sequence after an agent change:** package (captures `VERSION_ID`) → targeted apply with both code vars (runtime recreation is expected; capture the ARN from `terraform output`) → `gh variable set QA_RUNTIME_ARN <arn> --repo amdhd/vesselAI` if the ARN changed → trigger the workflow.

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

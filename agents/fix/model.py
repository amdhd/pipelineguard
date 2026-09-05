"""
The Bedrock call, and the budget that bounds it.

NO AGENTCORE RUNTIME, DELIBERATELY. PLAN.md Phase 2: this agent reads findings
and source and emits string replacements -- text in, text out. It never looks at
the app, so a Browser tool would bill the vCPU/GB meter for a capability that is
never invoked; the compile-and-test gate runs in the job against the real
toolchain, so a Code Interpreter would be weaker than what already exists. A
direct InvokeModel from the runner needs no image, no ARM64 build, no runtime
lifecycle and no session billing.

THE FOURTH METER
----------------
A QA run bills on three meters and the PR comment reports all three. This adds a
fourth, and it is the only one that bills per ATTEMPT rather than per run: an
edit the allow-list rejects and an edit that fails to compile cost exactly what
a good one cost. So the budget is cumulative across findings and is checked at a
call boundary -- you cannot preempt a model mid-generation, which is the same
correction PLAN.md 1d makes for the QA side.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fix_schema  # noqa: E402
import prompt as fix_prompt  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")

# Must agree with fix_model_profile_ids in infra/modules/qa_agent/variables.tf.
# The IAM policy scopes to specific model ARNs, so a mismatch here is an
# AccessDenied at invoke time, not a fallback.
DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"

# MEASURED, on run 33409654638: one finding cost 29,402 tokens (29,028 in /
# 374 out) against a full 8-file selection. Rounded up for headroom.
MEASURED_TOKENS_PER_FINDING = 32_000

# How many findings one run will attempt. Beyond this, findings are SKIPPED WITH
# A REASON rather than silently dropped or half-attempted.
DEFAULT_MAX_FINDINGS = 5


def token_budget_for(max_findings: int) -> int:
    """
    A budget that can actually pay for the findings the run says it will attempt.

    DERIVED, not chosen, for the same reason agent.py derives its token budget
    from its route cap: two independently reasonable numbers describe an
    impossible run. The old flat 120,000 against a measured 29,402 per finding
    meant the budget ran out on the FIFTH finding of a five-finding run -- and
    before the fix in this commit, that discarded the four patches already
    applied. A budget and a work cap that can disagree will eventually disagree
    at the worst moment.
    """
    return int(max_findings * MEASURED_TOKENS_PER_FINDING * 1.25)


# ~200k at the default of five findings. At sonnet rates that is a worst case of
# roughly $0.45 for a run that attempts all five, which is the number to look at
# before raising the cap.
DEFAULT_TOKEN_BUDGET = token_budget_for(DEFAULT_MAX_FINDINGS)

# ANY info string, not just `json`. The QA agent's pattern only recognises
# ```json, which is fine for an agent that reports what it saw and never writes
# code. A CODE-FIXING agent emits ```tsx and ```ts constantly, and an
# unrecognised opener does not simply fail to match -- it desynchronises the
# pairing, so the regex pairs a CLOSING fence with the next OPENING one and
# captures the prose between two code blocks. Observed on run 33408195295: the
# model explained the bug in a ```tsx block, and "last fence wins" selected its
# explanation instead of its answer.
_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


class BudgetExhausted(Exception):
    """Raised at a call boundary when the cumulative token budget is spent."""


class Budget:
    """
    Cumulative token accounting.

    Checked BEFORE a call, never during. A model cannot be stopped
    mid-generation, so a budget that pretends otherwise reports a number it did
    not enforce -- PLAN.md 1d, applied to the fix side.
    """

    def __init__(self, token_budget: int = DEFAULT_TOKEN_BUDGET):
        self.token_budget = token_budget
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.calls = 0

    @property
    def spent(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def check(self) -> None:
        if self.spent >= self.token_budget:
            raise BudgetExhausted(
                f"token budget exhausted: {self.spent} of {self.token_budget} spent "
                f"over {self.calls} call(s)"
            )

    def record(self, usage: dict) -> None:
        self.calls += 1
        self.input_tokens += usage.get("inputTokens", 0) or 0
        self.output_tokens += usage.get("outputTokens", 0) or 0
        self.cache_read += usage.get("cacheReadInputTokens", 0) or 0
        self.cache_write += usage.get("cacheWriteInputTokens", 0) or 0


def client(region: str = DEFAULT_REGION):
    """
    A bedrock-runtime client with bounded retries.

    Three attempts, not the default ten: a run that has already spent its
    context on one finding should surface a throttle to the summary rather than
    silently absorbing minutes of backoff into the job's wall clock.
    """
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def extract_json(text: str) -> dict:
    """
    Parse the model's message as the response object.

    A fenced block is an explicit, self-delimiting payload the model chose to
    mark, so reading it is not inference -- while narration outside a fence is
    DISCARDED, never parsed. That much carries over from the QA agent.

    WHAT DOES NOT CARRY OVER IS "THE LAST FENCE WINS". That heuristic assumes
    the model emits at most one fenced block, which holds for an agent that
    reports observations and fails immediately for one that writes code: this
    model quotes the buggy source in a ```tsx block to explain itself, so the
    last fence is routinely its reasoning rather than its answer.

    So EVERY fence is a candidate, tried newest-first, and the first one that
    parses as an object carrying "edits" wins. That is still not inference --
    each candidate is a block the model delimited itself; the only change is
    that a wrong guess about WHICH block no longer discards a correct answer.

    A parsed object WITHOUT "edits" is kept as a fallback so schema validation
    can produce its own precise complaint ("missing required key") rather than
    this function reporting the less useful "no JSON found".
    """
    fences = _FENCE.findall(text)
    # Bare text last: a model that skipped the fence entirely is still readable,
    # and this costs nothing when the fences already parsed.
    candidates = [c.strip() for c in reversed(fences)] + [text.strip()]

    if not any(candidates):
        raise fix_schema.FixSchemaError("model returned no text at all")

    fallback = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "edits" in parsed:
            return parsed
        if isinstance(parsed, dict) and fallback is None:
            fallback = parsed

    if fallback is not None:
        return fallback

    error = fix_schema.FixSchemaError(
        f"no fenced block parsed as the response object. Received {len(text)} "
        f"chars in {len(fences)} fenced block(s); newest block starts: "
        f"{candidates[0][:300]!r}"
    )
    # The full text, so a failure is diagnosable from the run artefact instead
    # of by paying for the call again. Run 33408195295 cost $0.09 and left
    # nothing behind but a 300-character excerpt.
    error.raw = text
    raise error


def propose(
    finding: dict,
    selection: dict,
    *,
    bedrock,
    budget: Budget,
    model: str = DEFAULT_MODEL,
    max_tokens: int = fix_prompt.MAX_OUTPUT_TOKENS,
) -> dict:
    """
    One finding in, one validated response out.

    One call per finding, not one call for all of them. Findings are independent,
    a single oversized context makes every fix worse, and a malformed response
    for one finding would otherwise discard the work done on the others.
    """
    budget.check()

    message = fix_prompt.build(
        finding,
        selection["files"],
        excluded=selection.get("excluded"),
        contract=selection.get("contract"),
    )

    try:
        response = bedrock.converse(
            modelId=model,
            system=[{"text": fix_prompt.SYSTEM}],
            messages=[{"role": "user", "content": [{"text": message}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        logger.error("converse failed for %s: %s", finding.get("id"), code)
        raise

    budget.record(response.get("usage", {}))

    if response.get("stopReason") == "max_tokens":
        # Truncated JSON is not a partial answer, it is an invalid one. Say which
        # of the two fixes applies rather than leaving a parse error to be
        # misread as a prompt problem.
        raise fix_schema.FixSchemaError(
            f"model hit max_tokens ({max_tokens}) before closing its JSON. Either "
            "raise --max-output-tokens or the edit is too large for one pass."
        )

    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(block.get("text", "") for block in blocks)
    return fix_schema.validate(extract_json(text))

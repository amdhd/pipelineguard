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

# Cumulative input+output tokens across every finding in one run. Sized against
# the selection caps: MAX_BYTES is 96_000 (~24k input tokens) per finding, so
# this is roughly four full-context findings plus their outputs.
DEFAULT_TOKEN_BUDGET = 120_000

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


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

    Same treatment as the QA agent's `_extract_json`, and the reasoning
    transfers verbatim: a ```json fence is an explicit, self-delimiting payload
    the model chose to mark, so reading it is not inference -- while narration
    outside the fence is DISCARDED, never parsed. The last fence wins; an
    earlier one is usually the model quoting the schema back to itself.
    """
    fences = _FENCE.findall(text)
    candidate = fences[-1].strip() if fences else text.strip()

    if not candidate:
        raise fix_schema.FixSchemaError("model returned no text at all")

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise fix_schema.FixSchemaError(
            f"model output is not valid JSON: {e}. Received {len(text)} chars, "
            f"{len(fences)} fenced block(s); candidate starts: {candidate[:300]!r}"
        ) from e


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

    message = fix_prompt.build(finding, selection["files"], excluded=selection.get("excluded"))

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

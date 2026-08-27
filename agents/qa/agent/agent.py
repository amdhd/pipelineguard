"""
QA agent -- runs INSIDE AgentCore Runtime.

This is the agent, not the harness. It holds the rubric, opens the browser
session, and runs the tool-use loop. The harness (agents/qa/harness/) runs in the
GitHub runner, calls InvokeAgentRuntime, and validates what comes back. Two
programs, two places; see PLAN.md 1b.

Entry point contract, read from the starter toolkit's own packaging code rather
than from documentation: `entryPoint` is a COMMAND ARRAY -- ["agent.py"], or
["opentelemetry-instrument", "agent.py"] with observability -- and the module
must expose a BedrockAgentCoreApp named `app`. It is an ASGI server, not a
Lambda-style f(event, context).
"""

import json
import logging
import math
import re
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from bedrock_agentcore import BedrockAgentCoreApp

import browser_tools
import rubric
import schema

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("qa-agent")

app = BedrockAgentCoreApp()

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
REPORTS_BUCKET = os.environ.get("REPORTS_BUCKET", "")
METRIC_NAMESPACE = "PipelineGuard/QAAgent"

# Defaults. Every one is overridable per invoke so the workflow can dial cost
# without redeploying the agent (PLAN.md 1d).
DEFAULT_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_MAX_TOKENS_PER_CALL = 4096  # per Converse call
DEFAULT_DEADLINE_SECONDS = 600
DEFAULT_MAX_ROUTES = len(rubric.PRIVATE_ROUTES)

# Seconds held back from the deadline so the final report call can still be made
# after the loop stops. See Budget.reserve().
REPORT_RESERVE_SECONDS = 60

# Prompt caching. Verified against the Bedrock prompt-caching documentation, not
# assumed -- three of its rules shape the implementation below:
#
#   1. Checkpoints chain tools -> system -> messages, and the model's MINIMUM is
#      evaluated against the CUMULATIVE tokens across all three. This agent's
#      system prompt plus tool specs is ~2k tokens, under Haiku 4.5's 4,096
#      minimum, so a checkpoint sitting after the static content alone would
#      silently never cache on the default rung. It caches once the conversation
#      itself pushes the prefix past the minimum, which a rolling checkpoint at
#      the end of the messages does automatically.
#   2. For Claude models Bedrock offers SIMPLIFIED cache management: place ONE
#      checkpoint at the end, and it looks back ~20 content blocks for the
#      longest matching prefix. That removes the usual need to keep old
#      breakpoints alive by hand -- so this moves a single checkpoint each turn
#      rather than accumulating up to the 4 allowed.
#   3. An under-minimum checkpoint is NOT an error. Inference still succeeds and
#      the prefix simply is not cached, which is why the early turns of a run
#      cost the same as they did before and only the later ones get cheaper.
#
# TTL is left at the default 5 minutes: turns are seconds apart, and a 5-minute
# cache refreshed inside its window costs nothing extra to keep alive.
CACHE_POINT = {"cachePoint": {"type": "default"}}


def _place_cache_point(messages: list[dict]) -> list[dict]:
    """
    Move the single rolling cache checkpoint to the end of the history.

    Everything before it -- tools, system, and every prior turn -- becomes the
    cached prefix, so the next turn reads it back at the cache rate instead of
    re-paying full input price for the whole conversation. That is the entire
    saving: this loop re-sends its complete history on every single call, so
    without a checkpoint the same tokens are bought again at full price up to
    thirty times in one run.

    The old checkpoint is REMOVED as the new one is placed. Bedrock's lookback
    finds the previous boundary on its own, and leaving a trail of markers would
    spend the four-checkpoint allowance on positions nothing reads from.
    """
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [b for b in content if "cachePoint" not in b]

    if messages and isinstance(messages[-1].get("content"), list):
        messages[-1]["content"].append(dict(CACHE_POINT))
    return messages


# --- Deriving the token budget from the route cap ---------------------------
#
# CONTEXT GROWS QUADRATICALLY, and a flat budget cannot know that.
#
# Every turn re-sends the entire history, and each tool result carries up to
# browser_tools.MAX_TEXT_CHARS of page text (~1.5k tokens). So the CUMULATIVE
# input across N turns is not PER_TURN*N, it is roughly
#
#     BASE*N + PER_TURN*N*(N-1)/2
#
# The old defaults were a flat 200_000 tokens, 40 turns and 8 routes, chosen
# independently. Solving the above for 200k gives N ~= 15 turns -- while a login
# plus eight routes needs 25 or more. So the token cap fired MID-SWEEP on a
# default run, the turn cap at 40 was unreachable dead code, and the normal
# outcome of a full sweep was a truncated run. Three numbers that each looked
# reasonable alone described a run that could not finish.
#
# Deriving the budget from the route cap makes them unable to disagree: ask for
# fewer routes and the budget falls with them, which is exactly what the
# schedule trigger's reduced route set wants.
_BASE_TOKENS = 2_000  # system prompt + tool specs, re-sent every turn
_TOKENS_PER_TURN = 1_700  # one tool result (6k chars) + the assistant message
_TURNS_PER_ROUTE = 2.5  # navigate, then read/click to judge the page
_LOGIN_TURNS = 6  # navigate, type, type, click, verify, and one to spare
_HEADROOM = 1.25  # models are not perfectly efficient; do not cap them exactly


def turns_for(max_routes: int) -> int:
    """Turns a run needs to log in and cover `max_routes` routes."""
    return int((_LOGIN_TURNS + _TURNS_PER_ROUTE * max_routes) * _HEADROOM)


def token_budget_for(max_routes: int) -> int:
    """
    Cumulative token budget that lets a run of `max_routes` routes actually
    finish. Rounded UP to something a human can read in a log line.

    Up, not to nearest: rounding a budget down re-creates the bug this function
    exists to remove, just smaller. At four routes the model needs 363,000 and
    round-to-nearest budgets 360,000 -- a cap 3,000 short of the run it was
    derived from.
    """
    n = turns_for(max_routes)
    total = _BASE_TOKENS * n + _TOKENS_PER_TURN * n * (n - 1) / 2
    return int(math.ceil(total / 10_000) * 10_000)


DEFAULT_MAX_TURNS = turns_for(DEFAULT_MAX_ROUTES)
DEFAULT_TOKEN_BUDGET = token_budget_for(DEFAULT_MAX_ROUTES)


class Budget:
    """
    Turn, token and wall-clock caps.

    PLAN.md 1d, corrected: you cannot preempt a model mid-generation from
    outside, so 'abort when the budget is exceeded' is not implementable as
    stated. What IS: cap max_tokens per call, cap the loop, and check cumulative
    usage BETWEEN turns, stopping at the next turn boundary and reporting a
    partial run. The wall-clock deadline matters independently because browser
    memory bills on session duration including idle -- a wedged run costs money
    while producing no tokens.
    """

    def __init__(
        self,
        max_turns: int,
        token_budget: int,
        deadline_seconds: int,
        *,
        report_tokens: int = 0,
        report_seconds: int = 0,
    ):
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.deadline = time.monotonic() + deadline_seconds
        self.started = time.monotonic()
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        # Held back so the run can still ASK FOR ITS REPORT after stopping.
        # Zero means no reserve, which is what the caps mean on their own.
        self.report_tokens = report_tokens
        self.report_seconds = report_seconds
        self.last_input_tokens = 0

    def record(self, usage: dict) -> None:
        """
        WITH CACHING ON, `inputTokens` IS NOT THE INPUT.

        Bedrock reports it as the NON-cached portion only; the documented
        identity is

            total input = inputTokens + cacheReadInputTokens + cacheWriteInputTokens

        Summing `inputTokens` alone -- which is what this did before caching --
        would under-count a cached turn by an order of magnitude, so the token
        budget would stop binding, runs would grow until the wall-clock deadline
        caught them, and the browser meter would quietly collect the difference.
        A cheaper token is still a token, and the budget measures CONTEXT.
        """
        self.turns += 1
        cache_read = usage.get("cacheReadInputTokens", 0) or 0
        cache_write = usage.get("cacheWriteInputTokens", 0) or 0
        uncached = usage.get("inputTokens", 0)

        self.input_tokens += uncached
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.output_tokens += usage.get("outputTokens", 0)

        # The next call re-sends the whole history, so the last call's total
        # input is the best available estimate of what one more call will cost.
        self.last_input_tokens = uncached + cache_read + cache_write

    def reserve(self) -> int:
        """
        Tokens to keep in hand for the final report call.

        Self-adjusting rather than a constant: the cost of one more call is the
        current context (~= the last call's input) plus whatever the report
        itself is allowed to generate.
        """
        if not self.report_tokens:
            return 0
        return self.last_input_tokens + self.report_tokens

    def exhausted(self) -> str | None:
        """Returns a reason string when the loop must stop, else None."""
        if self.turns >= self.max_turns:
            return f"turn cap reached ({self.max_turns})"
        if self.total_tokens + self.reserve() >= self.token_budget:
            return f"token budget reached ({self.token_budget})"
        if time.monotonic() + self.report_seconds >= self.deadline:
            return "wall-clock deadline reached"
        return None

    @property
    def total_tokens(self) -> int:
        """
        Every token that crossed the wire, cached or not.

        Deliberately NOT the billed-token count: this is what bounds the run,
        and a cached token still occupies context, still takes time to process,
        and still holds the browser session open while it does.
        """
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    @property
    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started)


def _screenshot_sink(session_id: str):
    """Upload screenshots straight to S3 under the agent's own execution role."""
    s3 = boto3.client("s3", region_name=REGION)

    def sink(label: str, png: bytes) -> str:
        key = f"screenshots/{session_id}/{label}.png"
        if not REPORTS_BUCKET:
            logger.warning("REPORTS_BUCKET unset; screenshot %s not persisted", key)
            return key
        s3.put_object(Bucket=REPORTS_BUCKET, Key=key, Body=png, ContentType="image/png")
        return key

    return sink


def _archive(session_id: str, findings: dict) -> str | None:
    """
    Archive the findings JSON to S3.

    PLAN.md Observability: "Findings JSON archived to S3 per PR." Without it a
    completed run is unrecoverable -- the report goes to a GitHub step summary
    and nowhere else, so a green run three days ago cannot tell you what it
    actually found. That also makes the Phase 3 convergence check impossible,
    since it compares finding sets ACROSS rounds.

    Written from the RUNTIME's execution role, not the workflow's. That is what
    keeps the CI-reachable role at invoke-one-runtime, read-one-secret.
    """
    if not REPORTS_BUCKET:
        logger.warning("REPORTS_BUCKET unset; findings not archived")
        return None
    key = f"reports/{session_id}/findings.json"
    try:
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=REPORTS_BUCKET,
            Key=key,
            Body=json.dumps(findings, indent=2).encode(),
            ContentType="application/json",
        )
        logger.info("archived findings to s3://%s/%s", REPORTS_BUCKET, key)
        return key
    except Exception:  # noqa: BLE001 -- archiving must never fail a good run
        logger.warning("could not archive findings", exc_info=True)
        return None


def _presign(keys: list[str], expires: int) -> dict[str, str]:
    """
    Presigned GETs so a reviewer can actually open the evidence.

    The bucket is block-public-access, so a bare key in a PR comment is
    unopenable -- the evidence for every finding would be invisible to the person
    meant to act on it. Expiry is capped at the bucket's lifecycle so a link
    never outlives the object it points at.
    """
    if not REPORTS_BUCKET:
        return {}
    s3 = boto3.client("s3", region_name=REGION)
    urls = {}
    for key in keys:
        try:
            urls[key] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": REPORTS_BUCKET, "Key": key},
                ExpiresIn=expires,
            )
        except Exception:  # noqa: BLE001 -- a missing link must not fail the run
            logger.warning("could not presign %s", key, exc_info=True)
    return urls


def _emit_metrics(budget: Budget, model: str) -> None:
    """Token and session-second metrics. Never fail the run over telemetry."""
    try:
        boto3.client("cloudwatch", region_name=REGION).put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": "InputTokens", "Value": budget.input_tokens, "Unit": "Count"},
                {"MetricName": "OutputTokens", "Value": budget.output_tokens, "Unit": "Count"},
                # Worth a metric of their own: a run where CacheReadTokens stays
                # flat at zero across turns is a cache that has silently stopped
                # matching, which costs ~10x and shows up nowhere else.
                {"MetricName": "CacheReadTokens", "Value": budget.cache_read_tokens, "Unit": "Count"},
                {"MetricName": "CacheWriteTokens", "Value": budget.cache_write_tokens, "Unit": "Count"},
                {"MetricName": "SessionSeconds", "Value": budget.elapsed_seconds, "Unit": "Seconds"},
                {"MetricName": "Turns", "Value": budget.turns, "Unit": "Count"},
            ],
        )
    except Exception:  # noqa: BLE001
        logger.warning("metric publish failed", exc_info=True)


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict:
    """
    Parse the model's final message as the findings object.

    POSITION REVERSED, and the reason matters. This used to strip a fence only
    when the message STARTED with one, on the stated principle that hunting for
    JSON inside prose hides a prompt bug rather than fixing it.

    A real run disproved it. After eleven turns of tool use the model returned:

        I'm being redirected again... this is expected behavior and not a
        finding. I've now tested 3 routes as per the limit... Let me compile
        the results.

        ```json
        { ...a complete, schema-valid findings object... }
        ```

    The findings were CORRECT. The parser threw them away.

    The original directive guards against INFERRING structure from unstructured
    text -- that is what turned "Let me verify this further" into a HIGH-severity
    row in the reference implementation. A ```json fence is not unstructured
    text. It is an explicit, self-delimiting payload the model chose to mark, and
    reading it is not inference.

    The practical case is stronger still: models narrate, especially deep in a
    tool loop. Enforcing "no preamble" by prompt alone makes the gate FLAKY --
    passing on some runs and discarding good findings on others -- and this
    plan's own reasoning is that a non-deterministic gate is worse than none,
    because it trains people to bypass it.

    What has NOT changed, and is the part that actually mattered: narration is
    DISCARDED, never parsed. Only a schema-valid object survives, so prose can
    still never become a finding.
    """
    fences = _FENCE.findall(text)
    if fences:
        # The LAST fence. An earlier one may be the model quoting the schema back
        # to itself before filling it in.
        candidate = fences[-1].strip()
    else:
        candidate = text.strip()

    if not candidate:
        raise schema.SchemaError(
            "model returned NO TEXT at all (empty content). Usually the loop "
            "ended on a stop reason that carried no final message."
        )

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        # Carry a snippet of what was ACTUALLY received. Without it a schema
        # violation is undiagnosable: "Expecting value: line 1 column 1" reads
        # the same whether the model narrated, returned nothing, or was cut off
        # mid-JSON -- and those need three different fixes.
        raise schema.SchemaError(
            f"model output is not valid JSON: {e}. Received {len(text)} chars, "
            f"{len(fences)} fenced block(s); candidate starts: {candidate[:300]!r}"
        ) from e


def _final_report(bedrock, model, system, messages, budget, max_tokens, stop_reason):
    """
    Ask for the report AFTER the budget stops the loop. Returns a validated
    findings object, or None if nothing usable came back.

    WHY THIS EXISTS. A truncated run used to return `"findings": []` with a
    label -- so a run that stopped after finding four defects reported none of
    them. The findings only ever exist in the model's FINAL message, and the old
    code stopped the loop without ever asking for one, throwing away everything
    the run had paid for. Budget.reserve() holds back exactly enough for this
    call, which is why stopping early is what makes reporting possible.

    Called with the browser session already closed: this is a text-only call, and
    the browser meter bills on wall-clock including idle, so there is no reason
    to hold the session open across it.
    """
    nudge = {
        "text": (
            f"STOP -- the run budget is exhausted ({stop_reason}). Do not call "
            "any more tools; any further tool call is discarded and the run is "
            "recorded as producing nothing. Emit your findings report NOW, as a "
            "single JSON object in a ```json fenced block, covering only what "
            "you have already observed. Set pages_tested to the number of routes "
            "you actually visited, not the number you intended to."
        )
    }
    # Converse requires alternating roles, and the loop always stops with a user
    # message of tool results at the tail -- so this appends a text block to that
    # message rather than adding a second user turn, which would be rejected.
    if messages and messages[-1].get("role") == "user":
        messages[-1]["content"].append(nudge)
    else:
        messages.append({"role": "user", "content": [nudge]})

    try:
        response = bedrock.converse(
            modelId=model,
            system=[{"text": system}],
            messages=_place_cache_point(messages),
            inferenceConfig={"maxTokens": max_tokens},
            # toolConfig is still sent. Converse rejects a history containing
            # toolUse/toolResult blocks when no tool config is present, so
            # dropping it here would fail the call outright; the instruction
            # above is what stops the model using them.
            toolConfig={"tools": browser_tools.tool_specs()},
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        logger.error("final report call failed: %s", code)
        return None

    budget.record(response.get("usage", {}))
    if response.get("stopReason") == "tool_use":
        # It kept exploring instead of reporting. Nothing to salvage, and saying
        # so beats presenting an empty findings list as a result.
        logger.warning("model called a tool instead of reporting; nothing salvaged")
        return None

    try:
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        return schema.validate(_extract_json(text))
    except (schema.SchemaError, KeyError) as e:
        logger.warning("final report was not usable: %s", e)
        return None


def run_qa(payload: dict) -> dict:
    session_id = payload.get("session_id") or f"run-{int(time.time())}"
    base_url = payload["target_url"]
    email = payload["email"]
    password = payload["password"]

    model = payload.get("model", DEFAULT_MODEL)
    max_routes = int(payload.get("max_routes", DEFAULT_MAX_ROUTES))
    ai_fallback = bool(payload.get("ai_fallback_mode", True))
    presign_expires = int(payload.get("presign_expires", 7 * 24 * 3600))
    # localStorage key the target writes its session token to. Configurable
    # because a different app authenticates differently; empty disables the
    # check rather than guessing.
    auth_token_key = payload.get("auth_token_key", "vm_token")
    max_tokens_per_call = int(payload.get("max_tokens_per_call", DEFAULT_MAX_TOKENS_PER_CALL))

    # Both caps default to a value DERIVED from max_routes, so asking for fewer
    # routes lowers the budget with them and the two can never disagree.
    budget = Budget(
        max_turns=int(payload.get("max_turns", turns_for(max_routes))),
        token_budget=int(payload.get("token_budget", token_budget_for(max_routes))),
        deadline_seconds=int(payload.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
        report_tokens=max_tokens_per_call,
        report_seconds=REPORT_RESERVE_SECONDS,
    )
    logger.info(
        "budget: %d routes -> %d turns, %d tokens (reserving one call for the report)",
        max_routes, budget.max_turns, budget.token_budget,
    )

    system = rubric.build_system_prompt(ai_fallback_mode=ai_fallback, max_routes=max_routes)

    # Adaptive retries with a generous attempt count. The first real run died on
    # ThrottlingException after botocore's default 4 tries: a browser-driving
    # agent sends large multi-turn requests in quick succession, which is exactly
    # the shape Bedrock throttles. Adaptive mode adds client-side rate limiting
    # rather than just retrying harder, so a throttled run slows down instead of
    # hammering and failing.
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )

    from bedrock_agentcore.tools.browser_client import browser_session

    stop_reason = None
    authenticated = None
    findings: dict = {}
    messages: list[dict] = []
    session = browser_tools.BrowserSession(
        base_url, _screenshot_sink(session_id), max_routes=max_routes
    )

    with browser_session(region=REGION) as client:
        ws_url, headers = client.generate_ws_headers()
        session.attach(ws_url, headers)
        try:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                f"Target: {base_url}\n"
                                f"Credentials: {email} / {password}\n\n"
                                "Log in, then work through the route list. Return the JSON object."
                            )
                        }
                    ],
                }
            )

            while True:
                reason = budget.exhausted()
                if reason:
                    stop_reason = reason
                    logger.warning("stopping: %s", reason)
                    break

                try:
                    response = bedrock.converse(
                        modelId=model,
                        system=[{"text": system}],
                        messages=_place_cache_point(messages),
                        inferenceConfig={"maxTokens": max_tokens_per_call},
                        toolConfig={"tools": browser_tools.tool_specs()},
                    )
                except ClientError as e:
                    # Even exhausted retries must not become a 500. The harness
                    # cannot tell a crashed runtime from a broken application,
                    # and on a PR those want different responses -- so a model
                    # failure ends the run as a labelled PARTIAL result carrying
                    # whatever was already learned.
                    code = e.response.get("Error", {}).get("Code", "ClientError")
                    stop_reason = f"model call failed: {code}"
                    logger.error("converse failed after retries: %s", code)
                    break
                budget.record(response.get("usage", {}))
                out = response["output"]["message"]
                messages.append(out)

                if response.get("stopReason") != "tool_use":
                    text = "".join(b.get("text", "") for b in out["content"])
                    logger.info(
                        "loop ended: stopReason=%s turns=%d blocks=%s text_chars=%d",
                        response.get("stopReason"),
                        budget.turns,
                        [next(iter(b), "?") for b in out["content"]],
                        len(text),
                    )
                    if response.get("stopReason") == "max_tokens":
                        # Truncated mid-generation: the JSON is real but cut off.
                        # Reporting it as "invalid JSON" would send the reader to
                        # the rubric when the fix is max_tokens_per_call.
                        raise schema.SchemaError(
                            "model hit max_tokens before finishing its JSON "
                            f"({max_tokens_per_call} per call). Raise "
                            "max_tokens_per_call or reduce max_routes."
                        )
                    findings = schema.validate(_extract_json(text))
                    break

                results = []
                for block in out["content"]:
                    if "toolUse" not in block:
                        continue
                    use = block["toolUse"]
                    result = session.dispatch(use["name"], use.get("input", {}))
                    results.append(
                        {
                            "toolResult": {
                                "toolUseId": use["toolUseId"],
                                "content": [{"json": result}],
                            }
                        }
                    )
                messages.append({"role": "user", "content": results})

        finally:
            # INSIDE the session, before close(). localStorage lives in the
            # browser, so the probe has to run while the socket is still open --
            # an earlier version put this after the `with` block and every run
            # died on "socket is already closed", including the comment claiming
            # it ran before teardown.
            authenticated = session.is_authenticated(auth_token_key)
            session.close()

    if stop_reason is not None:
        # A truncated run reports WHAT IT FOUND, labelled -- not an empty list.
        # Done here, outside the browser session, so the salvage call does not
        # bill browser wall-clock.
        # Two cases where asking is provably wasted spend:
        #   * the model itself is what failed -- another call almost certainly
        #     fails the same way, and each retry burns the deadline;
        #   * the run never authenticated -- every finding is about the login
        #     page and gets discarded below, so paying for a report is paying
        #     for something already known to be worthless.
        salvaged = None
        if authenticated is not False and not stop_reason.startswith("model call failed"):
            salvaged = _final_report(
                bedrock, model, system, messages, budget, max_tokens_per_call, stop_reason
            )
        findings = salvaged or {"overall": "FAIL", "pages_tested": 0, "findings": []}
        findings["incomplete"] = True
        findings["stop_reason"] = stop_reason
        findings["report_salvaged"] = salvaged is not None

    keys = [s["key"] for s in session.screenshots]
    urls = _presign(keys, presign_expires)
    for finding in findings.get("findings", []):
        shot = finding.get("screenshot")
        if isinstance(shot, dict) and shot.get("key") in urls:
            shot["url"] = urls[shot["key"]]

    _emit_metrics(budget, model)

    findings["session_seconds"] = budget.elapsed_seconds
    findings["cost"] = {
        "model_tokens": {
            # `input` is the UNCACHED input only, matching how Bedrock reports
            # it. The two cache counters are the rest of the input, billed at
            # different rates -- keeping them apart is what lets the harness
            # price them correctly instead of averaging three rates into one
            # wrong number.
            "input": budget.input_tokens,
            "output": budget.output_tokens,
            "cache_read": budget.cache_read_tokens,
            "cache_write": budget.cache_write_tokens,
        },
        "turns": budget.turns,
        "model": model,
        # Dollar figures are computed by the harness, which knows the price
        # table. The agent reports units; converting units to money in two places
        # is how the two disagree.
        "excludes": ["S3 storage", "CloudWatch Logs", "GitHub runner minutes"],
    }
    findings["screenshots"] = [{**s, "url": urls.get(s["key"])} for s in session.screenshots]
    findings["routes_visited"] = list(session.visited)
    findings["authenticated"] = authenticated

    # pages_tested is the model's own count and it has been wrong: a real run
    # reported 4 while the enforced counter recorded 3. Prefer the measured
    # value, and keep the claim beside it rather than silently overwriting.
    if authenticated is not None and session.visited:
        findings["pages_tested_reported_by_model"] = findings.get("pages_tested")
        findings["pages_tested"] = len(session.visited)

    if authenticated is False:
        # Every private-route observation in this run is worthless: PrivateRoute
        # redirects an unauthenticated visitor, so the agent was looking at the
        # login page while believing it was testing the app. Reporting that as a
        # clean PASS would be the worst possible output.
        logger.error("agent never authenticated; discarding findings")
        return {
            "error": "unauthenticated",
            "detail": (
                "The agent never authenticated -- no session token was present "
                f"under localStorage[{auth_token_key!r}] at the end of the run. "
                "Every private route redirects when unauthenticated, so any "
                "finding (or absence of findings) describes the login page, not "
                "the application."
            ),
            "findings": [],
            "routes_visited": list(session.visited),
            "session_seconds": budget.elapsed_seconds,
            "archive_key": _archive(session_id, {"error": "unauthenticated", "routes_visited": list(session.visited)}),
        }

    # Archive LAST, so the stored copy is the complete one the harness receives.
    archived = _archive(session_id, findings)
    if archived:
        findings["archive_key"] = archived
    return findings


@app.entrypoint
def invoke(payload: dict) -> dict:
    """
    AgentCore entrypoint.

    A schema violation is returned as a structured failure rather than raised:
    the harness must be able to tell "the agent ran and produced nonsense" from
    "the agent crashed", and those want different responses on a PR.
    """
    try:
        return run_qa(payload)
    except schema.SchemaError as e:
        logger.error("schema violation: %s", e)
        return {"error": "schema_violation", "detail": str(e), "findings": []}
    except KeyError as e:
        return {"error": "bad_payload", "detail": f"missing required field {e}", "findings": []}


if __name__ == "__main__":  # pragma: no cover
    app.run()

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
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_TOKENS_PER_CALL = 4096  # per Converse call
DEFAULT_TOKEN_BUDGET = 200_000  # cumulative across the run
DEFAULT_DEADLINE_SECONDS = 600
DEFAULT_MAX_ROUTES = len(rubric.PRIVATE_ROUTES)


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

    def __init__(self, max_turns: int, token_budget: int, deadline_seconds: int):
        self.max_turns = max_turns
        self.token_budget = token_budget
        self.deadline = time.monotonic() + deadline_seconds
        self.started = time.monotonic()
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def record(self, usage: dict) -> None:
        self.turns += 1
        self.input_tokens += usage.get("inputTokens", 0)
        self.output_tokens += usage.get("outputTokens", 0)

    def exhausted(self) -> str | None:
        """Returns a reason string when the loop must stop, else None."""
        if self.turns >= self.max_turns:
            return f"turn cap reached ({self.max_turns})"
        if self.total_tokens >= self.token_budget:
            return f"token budget reached ({self.token_budget})"
        if time.monotonic() >= self.deadline:
            return "wall-clock deadline reached"
        return None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

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
                {"MetricName": "SessionSeconds", "Value": budget.elapsed_seconds, "Unit": "Seconds"},
                {"MetricName": "Turns", "Value": budget.turns, "Unit": "Count"},
            ],
        )
    except Exception:  # noqa: BLE001
        logger.warning("metric publish failed", exc_info=True)


def _extract_json(text: str) -> dict:
    """
    Parse the model's final message as the findings object.

    Prime directive 6: unparsed output is a FAILED RUN, not findings. This
    tolerates a markdown fence, because that is a formatting slip rather than the
    model narrating -- but it does NOT go hunting for JSON inside prose. If the
    model wrote an essay, that is a prompt bug to fix, not a payload to salvage.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError as e:
        raise schema.SchemaError(f"model output is not valid JSON: {e}") from e


def run_qa(payload: dict) -> dict:
    session_id = payload.get("session_id") or f"run-{int(time.time())}"
    base_url = payload["target_url"]
    email = payload["email"]
    password = payload["password"]

    model = payload.get("model", DEFAULT_MODEL)
    max_routes = int(payload.get("max_routes", DEFAULT_MAX_ROUTES))
    ai_fallback = bool(payload.get("ai_fallback_mode", True))
    presign_expires = int(payload.get("presign_expires", 7 * 24 * 3600))
    max_tokens_per_call = int(payload.get("max_tokens_per_call", DEFAULT_MAX_TOKENS_PER_CALL))

    budget = Budget(
        max_turns=int(payload.get("max_turns", DEFAULT_MAX_TURNS)),
        token_budget=int(payload.get("token_budget", DEFAULT_TOKEN_BUDGET)),
        deadline_seconds=int(payload.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
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
    session = browser_tools.BrowserSession(base_url, _screenshot_sink(session_id))

    with browser_session(region=REGION) as client:
        ws_url, headers = client.generate_ws_headers()
        session.attach(ws_url, headers)
        try:
            messages = [
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
            ]

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
                        messages=messages,
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

            if stop_reason is not None:
                # A truncated run reports what it has, labelled. Silently
                # returning partial findings as if complete would be worse than
                # failing: the PR comment would understate coverage.
                findings = {
                    "overall": "FAIL",
                    "pages_tested": 0,
                    "findings": [],
                    "incomplete": True,
                    "stop_reason": stop_reason,
                }
        finally:
            session.close()

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
            "input": budget.input_tokens,
            "output": budget.output_tokens,
        },
        "turns": budget.turns,
        "model": model,
        # Dollar figures are computed by the harness, which knows the price
        # table. The agent reports units; converting units to money in two places
        # is how the two disagree.
        "excludes": ["S3 storage", "CloudWatch Logs", "GitHub runner minutes"],
    }
    findings["screenshots"] = [{**s, "url": urls.get(s["key"])} for s in session.screenshots]
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

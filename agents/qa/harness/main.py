"""
QA harness CLI -- runs in the GitHub runner, NOT in AgentCore.

NAMED main.py, NOT handler.py. The gates use handler.py and this module would
have collided with them in the test session: both gate packages already put a
`handler` module on sys.path, so `import handler` here resolved to whichever one
pytest had imported first. pytest.ini's --import-mode=importlib fixes that for
identically-named TEST files; it does nothing for identically-named source
modules. "main" is also the more honest name -- this is an argparse CLI, not a
Lambda handler.

Two programs, two places (PLAN.md 1b). The agent holds the rubric and drives the
browser inside AgentCore Runtime. This half invokes it, re-validates what comes
back, prices it, and renders the comment. It holds no rubric and makes no model
calls of its own.

It performs NO AWS WRITES. Archiving and metrics happen inside the runtime's
execution role, which is what keeps the role a public repo's CI can assume down
to two statements: invoke one runtime, read one secret.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import report  # noqa: E402
import schema  # noqa: E402  -- the agent's validator, reused deliberately

DEFAULT_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")

# InvokeAgentRuntime requires a session id of at least 33 characters.
_MIN_SESSION_ID = 33


def new_session_id(prefix: str = "qa") -> str:
    """A session id that satisfies the API's minimum length."""
    sid = f"{prefix}-{uuid.uuid4().hex}"
    return sid.ljust(_MIN_SESSION_ID, "0")


def read_credentials(secret_arn: str, region: str = DEFAULT_REGION) -> dict:
    """
    Fetch the QA target's login from Secrets Manager.

    This is the harness's ONLY AWS read beyond the invoke, and the only reason
    its role needs secretsmanager at all: the health gate has to prove a real
    login works before an agent session is ever paid for.
    """
    client = boto3.client("secretsmanager", region_name=region)
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])


def invoke(runtime_arn: str, payload: dict, *, region: str = DEFAULT_REGION, session_id: str | None = None) -> dict:
    """
    Call the runtime and parse its response.

    A non-JSON body is treated as a failed run rather than an exception, so the
    caller can tell "the agent produced nonsense" from "the harness crashed" --
    the same distinction the agent's own entrypoint preserves.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id or new_session_id(),
        payload=json.dumps(payload).encode(),
        contentType="application/json",
    )
    body = response["response"].read()
    if isinstance(body, bytes):
        body = body.decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {
            "error": "invalid_runtime_response",
            "detail": f"runtime returned non-JSON ({len(body)} bytes)",
            "findings": [],
        }


def validate(findings: dict) -> dict:
    """
    Re-validate on receipt.

    The agent already validated before returning. Doing it again here is
    deliberate belt-and-braces across a trust boundary, not redundancy: this
    process is what posts to a PR, and it should not take the other side's word
    for the shape of what it is about to publish.
    """
    if findings.get("error"):
        return findings
    if findings.get("incomplete"):
        return findings
    try:
        schema.validate(findings)
    except schema.SchemaError as e:
        return {"error": "schema_violation", "detail": str(e), "findings": []}
    return findings


def run(args) -> int:
    creds = {}
    if args.secret_arn:
        creds = read_credentials(args.secret_arn, args.region)

    payload = {
        "session_id": args.session_label,
        "target_url": args.target_url,
        "email": args.email or creds.get("QA_TARGET_EMAIL", ""),
        "password": args.password or creds.get("QA_TARGET_PASSWORD", ""),
        "ai_fallback_mode": not args.live_ai,
    }
    for key, value in (
        ("model", args.model),
        ("max_turns", args.max_turns),
        ("token_budget", args.token_budget),
        ("deadline_seconds", args.deadline_seconds),
        ("max_routes", args.max_routes),
    ):
        if value is not None:
            payload[key] = value

    findings = validate(invoke(args.runtime_arn, payload, region=args.region))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=2))

    comment = report.render(
        findings, target_url=args.target_url, runner_minutes=args.runner_minutes
    )
    if args.comment_out:
        Path(args.comment_out).write_text(comment)
    else:
        print(comment)

    return report.exit_code(findings)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Invoke the QA agent and render its report.")
    p.add_argument("--runtime-arn", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--secret-arn", help="Secrets Manager ARN holding the QA target login")
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--session-label", default="qa-run")
    p.add_argument("--model")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--token-budget", type=int)
    p.add_argument("--deadline-seconds", type=int)
    p.add_argument("--max-routes", type=int)
    p.add_argument("--runner-minutes", type=int)
    p.add_argument("--json-out", help="Write the raw findings JSON here")
    p.add_argument("--comment-out", help="Write the rendered comment here instead of stdout")
    # Default is the DUMMY key path (PLAN.md Phase 0.5 #4): a live billable
    # credential behind a public tunnel is a spend liability in two directions.
    p.add_argument(
        "--live-ai",
        action="store_true",
        help="Target is running with a REAL AI key; tell the rubric AI answers are live.",
    )
    return p


def main(argv=None) -> int:  # pragma: no cover -- thin CLI wrapper
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

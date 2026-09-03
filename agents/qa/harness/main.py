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
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import report  # noqa: E402
import schema  # noqa: E402  -- the agent's validator, reused deliberately

# converge's re-verify identity is imported lazily in _reverify_rows, not here:
# the harness must keep running even where the converge package is absent. Put
# its directory on sys.path up front so that lazy import actually resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "converge"))

DEFAULT_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")

# InvokeAgentRuntime requires a session id of at least 33 characters.
_MIN_SESSION_ID = 33

# The harness waits for the agent's response, which arrives only when the run
# finishes. InvokeAgentRuntime is synchronous and the agent may legitimately
# take minutes -- it drives a browser and paces itself against a 10 requests-per
# -minute quota. The agent's own wall-clock deadline (600s default) is the
# largest amount of time a conforming run takes to produce its response, so the
# read timeout is derived from the deadline the caller actually configured plus
# a fixed startup allowance, clamped to 900s -- the ceiling for a synchronous
# invocation, past which nothing useful can arrive.
_INVOKE_DEADLINE_DEFAULT = 600  # mirrors agent.DEFAULT_DEADLINE_SECONDS (drift-tested)
_INVOKE_STARTUP_SLACK = 60
_INVOKE_CEILING = 900


def _read_timeout(payload: dict) -> int:
    deadline = int(payload.get("deadline_seconds", _INVOKE_DEADLINE_DEFAULT))
    return min(_INVOKE_CEILING, deadline + _INVOKE_STARTUP_SLACK)


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


def _latest_key(namespace: str) -> str:
    """The PR-stable key every run on that PR archives under (D-3)."""
    return f"reports/{namespace}/latest/findings.json"


def fetch_prior_report(bucket: str, namespace: str, *, region: str = DEFAULT_REGION) -> dict | None:
    """
    Fetch the last report QA wrote for this PR, to re-verify against (D-3).

    Reads only -- the harness never writes; archiving happens under the runtime's
    execution role. ``None`` means "nothing to re-verify": a first run on a PR,
    an unreachable bucket, or a corrupted body must not fail the run, they just
    mean the comment renders without the ``🔁 Prior findings re-verified`` block.
    """
    if not bucket or not namespace:
        return None
    try:
        body = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket, Key=_latest_key(namespace)
        )["Body"].read()
    except (ClientError, BotoCoreError):
        # NoSuchKey on the first run is the common case, not an error.
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _reverify_rows(prior: dict | None, findings: dict) -> list | None:
    """
    Reconcile the current run against the prior report, or None when there is no
    prior to reconcile against. Lazily imports converge so a harness without it
    still renders (just without the re-verify block).
    """
    if not prior:
        return None
    try:
        import convergence
    except ImportError:  # pragma: no cover -- converge ships in this repo
        return None
    # D-3 stage: a plain re-run carries no fix signal, so an absent defect is
    # NOT_REPRODUCED, never FIXED. D-4 passes the fix agent's "applied" list.
    return convergence.verify_report(prior, findings, fix_intervened=False)


def invoke(runtime_arn: str, payload: dict, *, region: str = DEFAULT_REGION, session_id: str | None = None) -> dict:
    """
    Call the runtime and parse its response.

    A non-JSON body is treated as a failed run rather than an exception, so the
    caller can tell "the agent produced nonsense" from "the harness crashed" --
    the same distinction the agent's own entrypoint preserves.
    """
    # READ TIMEOUT DERIVED FROM THE AGENT'S DEADLINE, NOT A MAGIC NUMBER.
    #
    # InvokeAgentRuntime is synchronous and the agent may legitimately take
    # minutes -- it drives a browser and paces itself against a 10 requests-per
    # -minute quota. Botocore's default read timeout is far shorter, so a run
    # that was working fine surfaced here as `runtime_unavailable`: "Read
    # timeout on endpoint URL". That is the wrong diagnosis pointing at the
    # wrong place, and it discards a report the agent had already produced.
    #
    # The old hardcoded 900s was sized to the ceiling, not to the run: a caller
    # who raised --deadline-seconds toward the ceiling was cut off mid-flight --
    # discarding the very report this timeout exists to protect. The timeout now
    # tracks the deadline the caller actually configured (default 600) plus a
    # startup allowance, and clamps at 900s -- the synchronous ceiling, past
    # which timing out tells the truth: nothing useful can arrive after that.
    #
    # retries are disabled deliberately. A retry would start a SECOND agent
    # session -- a second browser, a second set of model calls, billed again --
    # while the first is still running. Whatever went wrong, doing it twice
    # concurrently is not the fix.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            read_timeout=_read_timeout(payload), connect_timeout=15,
            retries={"max_attempts": 0},
        ),
    )
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id or new_session_id(),
            payload=json.dumps(payload).encode(),
            contentType="application/json",
        )
    except (ClientError, BotoCoreError) as e:
        # The first real run died here. A 500 from the runtime surfaces as a
        # botocore exception, which escaped and killed the harness before it
        # could write a comment -- so the workflow had nothing to publish and
        # failed on a missing file, three steps away from the actual cause.
        #
        # Every failure mode must leave a renderable result behind. That is the
        # whole reason this returns a dict instead of raising.
        return {
            "error": "runtime_unavailable",
            "detail": str(e)[:500],
            "findings": [],
        }
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
    # A partial run is validated like any other. It used to be waved through on
    # the grounds that it carried nothing worth checking -- true only while a
    # budget-exhausted run returned an empty findings list. It now salvages a
    # real report before stopping, so the belt-and-braces check applies to it
    # for exactly the same reason it applies to a complete run: this process is
    # what posts to a PR.
    try:
        schema.validate(findings)
    except schema.SchemaError as e:
        return {"error": "schema_violation", "detail": str(e), "findings": []}
    return findings


def run(args) -> int:
    creds = {}
    if args.secret_arn:
        creds = read_credentials(args.secret_arn, args.region)

    # Read the last report for this PR before invoking, so the current run can
    # be re-verified against it. None on a first run or an unreachable bucket.
    prior = fetch_prior_report(args.reports_bucket, args.report_namespace, region=args.region)

    payload = {
        "session_id": args.session_label,
        "target_url": args.target_url,
        "email": args.email or creds.get("QA_TARGET_EMAIL", ""),
        "password": args.password or creds.get("QA_TARGET_PASSWORD", ""),
        "ai_fallback_mode": not args.live_ai,
    }
    if args.report_namespace:
        # The runtime archives this run under reports/{namespace}/latest as well
        # as its run-scoped key, so the NEXT run on this PR can re-verify it.
        payload["report_namespace"] = args.report_namespace
    for key, value in (
        ("model", args.model),
        ("max_turns", args.max_turns),
        ("token_budget", args.token_budget),
        ("deadline_seconds", args.deadline_seconds),
        ("max_routes", args.max_routes),
        ("max_tokens_per_call", args.max_tokens_per_call),
        ("requests_per_minute", args.requests_per_minute),
        ("presign_expires", args.presign_expires),
    ):
        if value is not None:
            payload[key] = value

    findings = validate(invoke(args.runtime_arn, payload, region=args.region))

    # PROVENANCE. Stamped by the harness, not the agent: the agent drives a
    # browser against a tunnel and has no idea what commit built the thing it is
    # looking at, while the workflow knows exactly.
    #
    # This exists because a findings JSON outlives the code it describes. Run
    # 33137979741 observed two defects on `qa-corpus-1`; three days later a fix
    # run replayed them against `main`, where one had already been fixed and the
    # other never existed. The fix agent produced a confident, compiling,
    # WRONG patch for both, because nothing in the file told it the report was
    # about different code.
    if args.target_commit:
        findings["observed_at_commit"] = args.target_commit

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=2))

    comment = report.render(
        findings,
        runner_minutes=args.runner_minutes,
        reverify_rows=_reverify_rows(prior, findings),
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
    p.add_argument(
        "--session-label",
        default=new_session_id("qa-run"),
        help="Name the run's S3 evidence prefix (screenshots/ and reports/). The "
        "default is unique per invocation, so two concurrent runs cannot "
        "overwrite each other's keys under screenshots/qa-run/; pass an explicit "
        "label only to override deliberately.",
    )
    p.add_argument("--model")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--token-budget", type=int)
    p.add_argument("--deadline-seconds", type=int)
    p.add_argument("--max-routes", type=int)
    p.add_argument(
        "--requests-per-minute",
        type=int,
        help="Model calls per minute the agent may make. Bedrock's request quota is "
        "far tighter than its token quota (10/min per rung here); 0 disables pacing.",
    )
    # The agent supports these and the harness could not reach them, which made
    # its own error message unactionable: a run that hit max_tokens told the
    # reader to "raise max_tokens_per_call" through a flag that did not exist.
    p.add_argument(
        "--max-tokens-per-call",
        type=int,
        help="Per-Converse-call output cap. Raise it if a run fails with the agent's "
        "'hit max_tokens before finishing its JSON' error.",
    )
    p.add_argument(
        "--presign-expires",
        type=int,
        help="Seconds a screenshot link stays valid. NOTE: the agent signs with the "
        "runtime's temporary credentials, so a link cannot outlive them (typically "
        "~1 hour) no matter what is set here.",
    )
    p.add_argument(
        "--target-commit",
        help="Commit SHA the target was built from. Recorded in the findings JSON as "
        "observed_at_commit so a later fix run can tell whether the report still "
        "describes the code in front of it.",
    )
    p.add_argument("--runner-minutes", type=int)
    p.add_argument(
        "--reports-bucket",
        default=os.environ.get("REPORTS_BUCKET", ""),
        help="S3 bucket holding archived QA reports. Required (with "
        "--report-namespace) to re-verify this run against the previous report "
        "on the same PR; the harness only reads it, it never writes.",
    )
    p.add_argument(
        "--report-namespace",
        help="PR-stable alias (e.g. 'pr-125') this run is archived under and the "
        "previous report is read from. When set it is forwarded to the runtime so "
        "the findings JSON lands at reports/<namespace>/latest/findings.json as well "
        "as the run-scoped key; a later run on the same PR fetches it and renders "
        "the `Prior findings re-verified` block.",
    )
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

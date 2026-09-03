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

It performs NO AWS WRITES in the D-1..D-3 steady state: archiving and metrics
happen inside the runtime's execution role, which is what keeps the role a
public repo's CI can assume down to two statements: invoke one runtime, read one
secret. D-4 adds two BEST-EFFORT ledger writes under reports/<pr>/latest/
(board.json, fix-verdict.json) so a later score.py pass can read the loop --
they may never fail a run (see _put_report_json).
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


def _fetch_json(bucket: str, key: str, *, region: str = DEFAULT_REGION) -> dict | None:
    """
    Read and parse one object, or None when it cannot be read as JSON.
    NoSuchKey on a first run is the common case, not an error, and a corrupted
    body (an S3 website's error page, say) is not this program's failure either:
    every caller treats None as "nothing there".
    """
    if not bucket or not key:
        return None
    try:
        body = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket, Key=key
        )["Body"].read()
    except (ClientError, BotoCoreError):
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _put_report_json(
    bucket: str, key: str, payload: dict, *, region: str = DEFAULT_REGION
) -> bool:
    """
    Best-effort S3 write for the D-4 ledger files (board.json, fix-verdict.json).

    The comment is the product; these files are how a later score.py pass reads
    what happened to the loop. A write that fails must never fail the run or
    discard a comment that landed, so every failure is swallowed and reported as
    False. Returns False without constructing a client when the run has no
    reports bucket.
    """
    if not bucket or not key:
        return False
    try:
        boto3.client("s3", region_name=region).put_object(
            Bucket=bucket, Key=key, Body=json.dumps(payload).encode()
        )
        return True
    except (ClientError, BotoCoreError):
        return False


def _pr_number(namespace: str | None) -> int | None:
    """'pr-125' -> 125; a non-PR namespace (or none) -> None."""
    if not namespace or not namespace.startswith("pr-"):
        return None
    try:
        return int(namespace[3:])
    except ValueError:
        return None


def _latest_fix_verdict_key(namespace: str) -> str:
    """The key a fix-PR QA run writes its verdict to in the ORIGIN namespace."""
    return f"reports/{namespace}/latest/fix-verdict.json"


def _latest_board_key(namespace: str) -> str:
    """The key a clean origin run writes its reconciliation board to."""
    return f"reports/{namespace}/latest/board.json"


def _parse_fix_origin(raw: str | None) -> dict | None:
    """
    The committed qa-fix-origin.json sidecar, read from the FIX_ORIGIN env var.

    The workflow loads the file into the env with a heredoc, so the harness
    still reads nothing from the checkout. None when absent or malformed -- a
    bad sidecar must not fail the run, it just means no fix chain is honoured
    (the run reconciles its own namespace, D-3 behaviour).
    """
    if not raw:
        return None
    try:
        origin = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(origin, dict) or not isinstance(origin.get("origin"), dict):
        return None
    if not isinstance(origin.get("applied_fingerprints"), list):
        return None
    return origin


def _origin_pr(fix_origin: dict) -> int | None:
    """The origin PR number carried by a fix-origin sidecar, or None."""
    raw = fix_origin.get("origin", {}).get("pr")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _applied_set(fix_origin: dict) -> set[str]:
    """The fingerprints the fix run claimed to address, as a set for membership."""
    return {str(fp) for fp in fix_origin.get("applied_fingerprints") or []}


def _honor_fix_origin(
    fix_origin: dict | None, report_namespace: str | None, head_ref: str
) -> bool:
    """
    Anti-taint gate (D-4): honour FIX_ORIGIN only on the two runs the fix chain
    actually touches -- a run on an ``agent-fix/`` head (the fix PR reconciling
    the ORIGIN report) or the origin PR's own later run once the fix has merged
    up into it (``origin.pr == own``). A sidecar that propagates to any other
    branch -- main included -- is ignored, so it can never make an unrelated PR
    reconcile somebody else's findings.
    """
    if not fix_origin or not report_namespace:
        return False
    own_pr = _pr_number(report_namespace)
    if own_pr is None:
        return False
    if _origin_pr(fix_origin) == own_pr:
        return True
    return head_ref.startswith("agent-fix/")


def fetch_prior_report(bucket: str, namespace: str, *, region: str = DEFAULT_REGION) -> dict | None:
    """
    Fetch the last report QA wrote for this PR, to re-verify against (D-3).

    Reads the PR-stable alias; the only writes this program ever makes are the
    two best-effort D-4 ledger files under the same namespace (board.json,
    fix-verdict.json) -- see _put_report_json. ``None`` means "nothing to
    re-verify": a first run on a PR, an unreachable bucket, or a corrupted body
    must not fail the run, they just mean the comment renders without the
    ``🔁 Prior findings re-verified`` block.
    """
    if not bucket or not namespace:
        return None
    return _fetch_json(bucket, _latest_key(namespace), region=region)


def _ledger_rows(reverify_rows: list) -> list:
    """
    Reverify rows in the machine-readable ledger vocabulary.

    verify_report's statuses already ARE that vocabulary ("still failing",
    "fixed", ...) -- the report renderer maps them to the commented, uppercase
    display words, while the ledger files (board.json / fix-verdict.json) keep
    the raw meaning. `.lower()` is a belt-and-braces guard against upstream
    capitalisation drift, nothing more.
    """
    return [
        {
            "fingerprint": r["fingerprint"],
            "severity": r["severity"],
            "page": r["page"],
            "summary": r["summary"],
            "status": r["status"].lower().replace("_", " "),
        }
        for r in reverify_rows
    ]


def _reverify_rows(
    prior: dict | None,
    findings: dict,
    *,
    fix_intervened: bool | set[str] | None = None,
) -> list | None:
    """
    Reconcile the current run against the prior report, or None when there is no
    prior to reconcile against. Lazily imports converge so a harness without it
    still renders (just without the re-verify block).

    `fix_intervened` is what separates a plain re-run from a fix-leg run: the
    fix agent's applied-fingerprint set (D-4). An absent prior finding is FIXED
    only when its fingerprint is in that set; absent with no fix signal is
    NOT_REPRODUCED, never FIXED.
    """
    if not prior:
        return None
    try:
        import convergence
    except ImportError:  # pragma: no cover -- converge ships in this repo
        return None
    return convergence.verify_report(prior, findings, fix_intervened=fix_intervened)


def _unmatched_rows(prior: dict | None, findings: dict) -> list:
    """
    Current findings with no counterpart in the prior report (D-4).

    Each is either a new defect or a regression introduced by this branch. An
    errored/incomplete current run proves nothing, so it returns [] -- absence
    of a re-measurement must not look like absence of new findings.
    """
    if not prior or not prior.get("findings"):
        return []
    try:
        import convergence
    except ImportError:  # pragma: no cover -- converge ships in this repo
        return []
    return convergence.unmatched_current(prior, findings)


def _board_rows(reverify_rows: list, fix_verdict_rows: list | None) -> list:
    """
    Origin-board rows from the re-verify ledger + the fix PR's verdict file.

    Attribution precedence (D-4): a prior finding present again is *still
    failing*, from this run's own eyes. One that is absent is *fixed*, and the
    row's `source` says whether the fix PR's QA run (fix-verdict.json) or this
    run's own reconcile against the merged sidecar's applied set established it.
    Absent with neither is *not reproduced*: absence alone is never a fix.
    """
    verdict = {r["fingerprint"]: r["status"] for r in fix_verdict_rows or []}
    rows = []
    for r in reverify_rows:
        fp = r["fingerprint"]
        status = r["status"].lower().replace("_", " ")
        source = (
            "fix-verdict" if status == "fixed" and verdict.get(fp) == "fixed" else "origin-run"
        )
        rows.append(
            {
                "fingerprint": fp,
                "severity": r["severity"],
                "page": r["page"],
                "summary": r["summary"],
                "status": status,
                "source": source,
                "label": None,  # score.py's human slot; null until a human writes it
            }
        )
    return rows


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

    # D-4 fix chain. The workflow loads the committed qa-fix-origin.json into the
    # FIX_ORIGIN env var (heredoc, so the harness still reads nothing from the
    # checkout). It is honoured only on the fix chain's own two runs -- the fix
    # PR itself, and the origin PR once the fix has merged up into it -- never on
    # an unrelated branch (anti-taint, _honor_fix_origin).
    fix_origin = _parse_fix_origin(os.environ.get("FIX_ORIGIN"))
    own_pr = _pr_number(args.report_namespace)
    fix_pr_run = False
    fix_intervened: bool | set[str] | None = None
    prior_namespace = args.report_namespace
    if _honor_fix_origin(
        fix_origin, args.report_namespace, os.environ.get("GITHUB_HEAD_REF", "")
    ):
        origin_pr = _origin_pr(fix_origin)
        if origin_pr != own_pr:
            # A run on the fix PR's own head: reconcile the ORIGIN report, and
            # let only the fix agent's applied fingerprints be called FIXED.
            fix_pr_run = True
            prior_namespace = f"pr-{origin_pr}"
        fix_intervened = _applied_set(fix_origin)

    # Read the report to re-verify against BEFORE invoking, so the current run
    # can be reconciled against it. On the fix leg that is the ORIGIN PR's last
    # report; otherwise this run's own PR's. None on a first run or an
    # unreachable bucket.
    prior = fetch_prior_report(args.reports_bucket, prior_namespace, region=args.region)

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

    reverify_rows = _reverify_rows(prior, findings, fix_intervened=fix_intervened)
    unmatched = _unmatched_rows(prior, findings)

    # The board is the CLOSING ledger of the origin PR: it replaces the plain
    # re-verify table on a run that is the origin's own (not a fix PR), has a
    # prior report to reconcile, did not error or stop early, and finds no
    # CRITICAL/HIGH still present. Its header is "Final" only when a row is
    # actually fixed -- a clean re-run with no fix chain must not overclaim.
    board_rows = None
    board_fix_verdict_key = None
    if (
        own_pr is not None
        and not fix_pr_run
        and prior
        and prior.get("findings")
        and reverify_rows
        and not findings.get("error")
        and not findings.get("incomplete")
    ):
        blocking_now = any(
            str(f.get("severity", "")).upper() in ("CRITICAL", "HIGH")
            for f in (findings.get("findings") or [])
        )
        if not blocking_now:
            fix_verdict = _fetch_json(
                args.reports_bucket,
                _latest_fix_verdict_key(args.report_namespace),
                region=args.region,
            )
            if fix_verdict:
                board_fix_verdict_key = _latest_fix_verdict_key(args.report_namespace)
            board_rows = _board_rows(reverify_rows, (fix_verdict or {}).get("rows"))

    comment = report.render(
        findings,
        runner_minutes=args.runner_minutes,
        reverify_rows=reverify_rows,
        board_rows=board_rows,
        unmatched=unmatched,
    )
    if args.comment_out:
        Path(args.comment_out).write_text(comment)
    else:
        print(comment)

    # D-4 ledger emitters -- best-effort, never fail the run. The comment above
    # is the product; these files are how a later score.py pass reads the loop.
    if fix_pr_run and prior and args.reports_bucket and args.report_namespace:
        origin_pr = _origin_pr(fix_origin)
        blocking_now = any(
            str(f.get("severity", "")).upper() in ("CRITICAL", "HIGH")
            for f in (findings.get("findings") or [])
        )
        # fix-#60 rule: even an errored fix-leg run leaves a verdict behind, all
        # rows unverified -- an empty re-measurement must not read as clean.
        _put_report_json(
            args.reports_bucket,
            _latest_fix_verdict_key(f"pr-{origin_pr}"),
            {
                "schema": "pipelineguard/fix-verdict/v1",
                "origin": {
                    "repo": (fix_origin.get("origin") or {}).get("repo"),
                    "pr": origin_pr,
                },
                "fix_pr": own_pr,
                "reports_bucket": args.reports_bucket,
                "prior_findings_key": _latest_key(f"pr-{origin_pr}"),
                "overall": "FAIL" if blocking_now else "PASS",
                "rows": _ledger_rows(reverify_rows) if reverify_rows else [],
            },
            region=args.region,
        )

    if board_rows is not None and args.reports_bucket and args.report_namespace:
        origin_meta = (fix_origin or {}).get("origin") or {}
        _put_report_json(
            args.reports_bucket,
            _latest_board_key(args.report_namespace),
            {
                "schema": "pipelineguard/board/v1",
                "origin": {"repo": origin_meta.get("repo"), "pr": own_pr},
                "run": {
                    "session": args.session_label,
                    "report_namespace": args.report_namespace,
                    "observed_at_commit": findings.get("observed_at_commit"),
                },
                "reports_bucket": args.reports_bucket,
                "prior_findings_key": _latest_key(args.report_namespace),
                "fix_verdict_key": board_fix_verdict_key,
                "overall": "PASS",
                "rows": board_rows,
            },
            region=args.region,
        )

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

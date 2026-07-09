"""
Cost Gate Lambda.

Downloads the Terraform plan JSON, runs Infracost, and blocks when the projected
monthly cost delta exceeds COST_THRESHOLD. Slack is notified either way.

Two invocation modes are supported:
  * CodePipeline custom action — the event carries "CodePipeline.job"; the result
    is signalled with put_job_success_result / put_job_failure_result.
  * CodeBuild direct invoke — the event carries {"plan_s3_bucket","plan_s3_key"};
    the handler returns {"gate_status": "passed"|"failed", ...} for the build to read.
"""

import json
import logging
import os
from typing import Any

import boto3

from infracost_runner import run_infracost

logger = logging.getLogger()
logger.setLevel(logging.INFO)

codepipeline = boto3.client("codepipeline")
s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")


def get_secrets() -> dict:
    """Fetch API keys from Secrets Manager — never hardcode these."""
    response = secretsmanager.get_secret_value(SecretId=os.environ["SECRETS_ARN"])
    return json.loads(response["SecretString"])


def lambda_handler(event: dict, context: Any) -> Any:
    """Entry point. Dispatches on the invocation shape."""
    if "CodePipeline.job" in event:
        return _handle_pipeline_job(event)
    return _handle_direct(event)


def _evaluate(plan_path: str, secrets: dict) -> dict[str, Any]:
    """Run Infracost, decide pass/block, notify Slack. Returns a summary dict."""
    threshold = float(os.environ.get("COST_THRESHOLD", "50"))
    result = run_infracost(plan_path=plan_path, api_key=secrets["INFRACOST_API_KEY"])
    monthly_delta = result.get("monthly_cost_delta", 0.0)
    blocked = monthly_delta > threshold
    logger.info(
        "Cost delta: $%.2f/month (threshold: $%.2f) -> %s",
        monthly_delta,
        threshold,
        "BLOCK" if blocked else "PASS",
    )

    if blocked:
        message = (
            f"❌ COST GATE BLOCKED\n"
            f"Projected monthly cost increase: ${monthly_delta:.2f}\n"
            f"Allowed threshold: ${threshold:.2f}\n"
            f"Top cost drivers: {json.dumps(result.get('top_resources', []), indent=2)}\n"
            f"Review your Terraform changes and right-size resources."
        )
        logger.warning(message)
    else:
        message = (
            f"✅ COST GATE PASSED\n"
            f"Projected monthly cost increase: ${monthly_delta:.2f} "
            f"(threshold: ${threshold:.2f})"
        )
        logger.info(message)

    _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), message, "blocked" if blocked else "passed")
    return {"blocked": blocked, "monthly_cost_delta": monthly_delta, "message": message}


def _handle_direct(event: dict) -> dict:
    """CodeBuild-driven invoke: read the plan from S3, return a gate_status."""
    try:
        secrets = get_secrets()
        bucket = event.get("plan_s3_bucket") or os.environ.get("ARTIFACT_BUCKET")
        key = event["plan_s3_key"]
        plan_path = "/tmp/plan.json"
        s3.download_file(bucket, key, plan_path)
        logger.info("Downloaded plan artifact from s3://%s/%s", bucket, key)

        summary = _evaluate(plan_path, secrets)
        return {
            "gate_status": "failed" if summary["blocked"] else "passed",
            "monthly_cost_delta": summary["monthly_cost_delta"],
        }
    except Exception as e:  # noqa: BLE001 — gate must never silently pass
        logger.error("Cost gate error: %s", e, exc_info=True)
        return {"gate_status": "failed", "error": str(e)}


def _handle_pipeline_job(event: dict) -> None:
    """CodePipeline custom-action invoke: signal pass/fail on the job."""
    job = event["CodePipeline.job"]
    job_id = job["id"]
    try:
        secrets = get_secrets()
        artifact = job["data"]["inputArtifacts"][0]
        bucket = artifact["location"]["s3Location"]["bucketName"]
        key = artifact["location"]["s3Location"]["objectKey"]

        plan_path = "/tmp/plan.json"
        s3.download_file(bucket, key, plan_path)
        logger.info("Downloaded plan artifact from s3://%s/%s", bucket, key)

        summary = _evaluate(plan_path, secrets)
        if summary["blocked"]:
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    "message": (
                        f"Cost gate blocked: ${summary['monthly_cost_delta']:.2f}/month "
                        f"exceeds threshold"
                    ),
                },
            )
        else:
            codepipeline.put_job_success_result(
                jobId=job_id,
                outputVariables={
                    "monthly_cost_delta": str(summary["monthly_cost_delta"]),
                    "gate_status": "passed",
                },
            )
    except Exception as e:  # noqa: BLE001 — gate must never silently pass
        logger.error("Cost gate error: %s", e, exc_info=True)
        codepipeline.put_job_failure_result(
            jobId=job_id,
            failureDetails={"type": "JobFailed", "message": f"Gate error: {e}"},
        )


def _notify_slack(webhook_url: str | None, message: str, status: str) -> None:
    """Post a coloured Slack attachment. Failures here never fail the gate."""
    if not webhook_url:
        return
    import urllib.request

    color = "#36a64f" if status == "passed" else "#cc0000"
    payload = json.dumps(
        {
            "attachments": [
                {
                    "color": color,
                    "title": "PipelineGuard — Cost Gate",
                    "text": message,
                    "footer": "pipelineguard",
                }
            ]
        }
    ).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.warning("Slack notification failed: %s", e)

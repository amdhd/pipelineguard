"""
Cost Gate Lambda — invoked as a CodePipeline custom action.

Downloads the Terraform plan JSON artifact from S3, runs Infracost against it,
checks if the projected monthly cost delta exceeds the threshold, and calls
PutJobSuccessResult or PutJobFailureResult accordingly.
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


def lambda_handler(event: dict, context: Any) -> None:
    """Entry point. Always signals pass/fail back to CodePipeline."""
    job = event["CodePipeline.job"]
    job_id = job["id"]

    try:
        secrets = get_secrets()
        threshold = float(os.environ.get("COST_THRESHOLD", "50"))

        # Locate the plan JSON artifact produced by the terraform plan stage.
        artifact = job["data"]["inputArtifacts"][0]
        bucket = artifact["location"]["s3Location"]["bucketName"]
        key = artifact["location"]["s3Location"]["objectKey"]

        plan_path = "/tmp/plan.json"
        s3.download_file(bucket, key, plan_path)
        logger.info("Downloaded plan artifact from s3://%s/%s", bucket, key)

        result = run_infracost(
            plan_path=plan_path,
            api_key=secrets["INFRACOST_API_KEY"],
        )

        monthly_delta = result.get("monthly_cost_delta", 0.0)
        logger.info(
            "Cost delta: $%.2f/month (threshold: $%.2f)", monthly_delta, threshold
        )

        if monthly_delta > threshold:
            message = (
                f"❌ COST GATE BLOCKED\n"
                f"Projected monthly cost increase: ${monthly_delta:.2f}\n"
                f"Allowed threshold: ${threshold:.2f}\n"
                f"Top cost drivers: {json.dumps(result.get('top_resources', []), indent=2)}\n"
                f"Review your Terraform changes and right-size resources."
            )
            logger.warning(message)
            _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), message, status="blocked")
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    "message": (
                        f"Cost gate blocked: ${monthly_delta:.2f}/month exceeds "
                        f"${threshold:.2f} threshold"
                    ),
                },
            )
        else:
            message = (
                f"✅ COST GATE PASSED\n"
                f"Projected monthly cost increase: ${monthly_delta:.2f} "
                f"(threshold: ${threshold:.2f})"
            )
            logger.info(message)
            _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), message, status="passed")
            codepipeline.put_job_success_result(
                jobId=job_id,
                outputVariables={
                    "monthly_cost_delta": str(monthly_delta),
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

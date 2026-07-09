"""
Security Gate Lambda — invoked as a CodePipeline custom action.

Runs Trivy against the Docker image in ECR and Checkov against Terraform files,
calls Claude to summarise the findings, posts the summary as a GitHub PR comment,
and blocks the pipeline on any HIGH or CRITICAL finding.
"""

import json
import logging
import os
from typing import Any

import boto3

from checkov_runner import run_checkov
from claude_summariser import summarise_findings
from github_commenter import post_pr_comment
from trivy_runner import run_trivy

logger = logging.getLogger()
logger.setLevel(logging.INFO)

codepipeline = boto3.client("codepipeline")
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
        user_params = json.loads(
            job["data"]["actionConfiguration"]["configuration"].get("UserParameters", "{}")
        )
        ecr_image_uri = user_params.get("ecr_image_uri", "")
        github_pr_number = user_params.get("pr_number")
        github_repo = user_params.get("github_repo")
        terraform_dir = user_params.get("terraform_dir", "/tmp/terraform")

        logger.info("Running Trivy scan on %s", ecr_image_uri)
        trivy_results = run_trivy(ecr_image_uri)

        logger.info("Running Checkov scan on %s", terraform_dir)
        checkov_results = run_checkov(terraform_dir)

        high_count = trivy_results.get("HIGH", 0) + checkov_results.get("HIGH", 0)
        critical_count = trivy_results.get("CRITICAL", 0) + checkov_results.get("CRITICAL", 0)
        blocked = (high_count + critical_count) > 0

        logger.info("Generating Claude security summary")
        summary = summarise_findings(
            trivy_results=trivy_results,
            checkov_results=checkov_results,
            anthropic_api_key=secrets["ANTHROPIC_API_KEY"],
        )

        if github_pr_number and github_repo:
            post_pr_comment(
                repo=github_repo,
                pr_number=github_pr_number,
                summary=summary,
                blocked=blocked,
                github_token=secrets.get("GITHUB_TOKEN"),
            )

        if blocked:
            logger.warning(
                "Security gate BLOCKED: %d CRITICAL, %d HIGH findings",
                critical_count,
                high_count,
            )
            _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), summary, status="blocked")
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    "message": (
                        f"Security gate: {critical_count} CRITICAL + {high_count} HIGH "
                        f"findings. See PR comment for details."
                    ),
                },
            )
        else:
            logger.info("Security gate PASSED")
            _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), summary, status="passed")
            codepipeline.put_job_success_result(
                jobId=job_id,
                outputVariables={
                    "high_count": str(high_count),
                    "critical_count": str(critical_count),
                    "gate_status": "passed",
                },
            )

    except Exception as e:  # noqa: BLE001 — gate must never silently pass
        logger.error("Security gate error: %s", e, exc_info=True)
        codepipeline.put_job_failure_result(
            jobId=job_id,
            failureDetails={"type": "JobFailed", "message": f"Gate error: {e}"},
        )


def _notify_slack(webhook_url: str | None, summary: str, status: str) -> None:
    """Post the summary to Slack. Failures here never fail the gate."""
    if not webhook_url:
        return
    import urllib.request

    color = "#36a64f" if status == "passed" else "#cc0000"
    payload = json.dumps(
        {
            "attachments": [
                {
                    "color": color,
                    "title": "PipelineGuard — Security Gate",
                    "text": summary[:2900],  # Slack 3000-char limit
                    "footer": "pipelineguard | trivy + checkov + claude",
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

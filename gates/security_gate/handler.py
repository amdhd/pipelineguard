"""
Security Gate Lambda.

Runs Trivy against the app image in ECR and Checkov against the Terraform files,
calls Claude to summarise, posts the summary to the PR + Slack, and blocks on any
HIGH or CRITICAL finding.

Two invocation modes:
  * CodePipeline custom action — event carries "CodePipeline.job"; result signalled
    with put_job_success_result / put_job_failure_result. Terraform files are read
    from the local path in UserParameters (terraform_dir).
  * CodeBuild direct invoke — event carries {"ecr_image_uri","terraform_s3_bucket",
    "terraform_s3_key","pr_number","github_repo"}; the Terraform is downloaded from
    S3 (the Lambda has no source checkout) and the handler returns a gate_status.
"""

import json
import logging
import os
import zipfile
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
s3 = boto3.client("s3")


def get_secrets() -> dict:
    """Fetch API keys from Secrets Manager — never hardcode these."""
    response = secretsmanager.get_secret_value(SecretId=os.environ["SECRETS_ARN"])
    return json.loads(response["SecretString"])


def lambda_handler(event: dict, context: Any) -> Any:
    """Entry point. Dispatches on the invocation shape."""
    if "CodePipeline.job" in event:
        return _handle_pipeline_job(event)
    return _handle_direct(event)


def _download_terraform(bucket: str, key: str, dest: str) -> None:
    """Download the zipped Terraform artifact from S3 and extract it for Checkov."""
    os.makedirs(dest, exist_ok=True)
    zip_path = "/tmp/terraform.zip"
    s3.download_file(bucket, key, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    logger.info("Extracted Terraform from s3://%s/%s to %s", bucket, key, dest)


def _evaluate(
    image_uri: str,
    terraform_dir: str,
    secrets: dict,
    pr_number: str | None,
    github_repo: str | None,
) -> dict[str, Any]:
    """Run both scans, summarise, notify, and decide. Fail-closed on scanner error."""
    logger.info("Running Trivy scan on %s", image_uri)
    trivy_results = run_trivy(image_uri)

    logger.info("Running Checkov scan on %s", terraform_dir)
    checkov_results = run_checkov(terraform_dir)

    high = trivy_results.get("HIGH", 0) + checkov_results.get("HIGH", 0)
    critical = trivy_results.get("CRITICAL", 0) + checkov_results.get("CRITICAL", 0)
    blocked = (high + critical) > 0
    logger.info("Findings: %d CRITICAL, %d HIGH -> %s", critical, high, "BLOCK" if blocked else "PASS")

    summary = summarise_findings(
        trivy_results=trivy_results,
        checkov_results=checkov_results,
        anthropic_api_key=secrets["ANTHROPIC_API_KEY"],
    )

    if pr_number and github_repo:
        post_pr_comment(
            repo=github_repo,
            pr_number=pr_number,
            summary=summary,
            blocked=blocked,
            github_token=secrets.get("GITHUB_TOKEN"),
        )

    _notify_slack(secrets.get("SLACK_WEBHOOK_URL"), summary, "blocked" if blocked else "passed")
    return {"blocked": blocked, "high": high, "critical": critical, "summary": summary}


def _handle_direct(event: dict) -> dict:
    """CodeBuild-driven invoke: fetch Terraform from S3, return a gate_status."""
    try:
        secrets = get_secrets()
        image_uri = event.get("ecr_image_uri", "")
        terraform_dir = "/tmp/terraform"
        tf_bucket = event.get("terraform_s3_bucket") or os.environ.get("ARTIFACT_BUCKET")
        tf_key = event.get("terraform_s3_key")
        if tf_key:
            _download_terraform(tf_bucket, tf_key, terraform_dir)

        summary = _evaluate(
            image_uri, terraform_dir, secrets, event.get("pr_number"), event.get("github_repo")
        )
        return {
            "gate_status": "failed" if summary["blocked"] else "passed",
            "critical": summary["critical"],
            "high": summary["high"],
        }
    except Exception as e:  # noqa: BLE001 — gate must never silently pass
        logger.error("Security gate error: %s", e, exc_info=True)
        return {"gate_status": "failed", "error": str(e)}


def _handle_pipeline_job(event: dict) -> None:
    """CodePipeline custom-action invoke: signal pass/fail on the job."""
    job = event["CodePipeline.job"]
    job_id = job["id"]
    try:
        secrets = get_secrets()
        user_params = json.loads(
            job["data"]["actionConfiguration"]["configuration"].get("UserParameters", "{}")
        )
        summary = _evaluate(
            user_params.get("ecr_image_uri", ""),
            user_params.get("terraform_dir", "/tmp/terraform"),
            secrets,
            user_params.get("pr_number"),
            user_params.get("github_repo"),
        )
        if summary["blocked"]:
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    "message": (
                        f"Security gate: {summary['critical']} CRITICAL + {summary['high']} HIGH "
                        f"findings. See PR comment for details."
                    ),
                },
            )
        else:
            codepipeline.put_job_success_result(
                jobId=job_id,
                outputVariables={
                    "high_count": str(summary["high"]),
                    "critical_count": str(summary["critical"]),
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

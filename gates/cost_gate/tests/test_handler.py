"""Unit tests for the cost gate handler and infracost parser."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the gate package importable and stub boto3 before importing handler.
GATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATE_DIR))


@pytest.fixture(autouse=True)
def _boto3_stub(monkeypatch):
    """Replace boto3 clients with mocks so no AWS calls are made."""
    import boto3

    clients = {
        "codepipeline": MagicMock(),
        "s3": MagicMock(),
        "secretsmanager": MagicMock(),
    }

    def fake_client(name, *args, **kwargs):
        return clients[name]

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:region:acct:secret:x")
    monkeypatch.setenv("COST_THRESHOLD", "50")

    # Pin this gate's dir at the front of sys.path and drop any cached gate
    # modules so we import THIS gate's handler (both gates use the name).
    monkeypatch.syspath_prepend(str(GATE_DIR))
    for mod in ("handler", "infracost_runner"):
        sys.modules.pop(mod, None)
    import handler  # noqa: E402

    clients["secretsmanager"].get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "INFRACOST_API_KEY": "ico-test",
                "SLACK_WEBHOOK_URL": "",
                "ANTHROPIC_API_KEY": "sk-ant-test",
            }
        )
    }
    yield handler, clients


def _event():
    return {
        "CodePipeline.job": {
            "id": "job-123",
            "data": {
                "inputArtifacts": [
                    {
                        "location": {
                            "s3Location": {"bucketName": "b", "objectKey": "plan.json"}
                        }
                    }
                ]
            },
        }
    }


def test_passes_when_under_threshold(monkeypatch, _boto3_stub):
    handler, clients = _boto3_stub
    monkeypatch.setattr(
        handler, "run_infracost", lambda **_: {"monthly_cost_delta": 10.0, "top_resources": []}
    )

    handler.lambda_handler(_event(), None)

    clients["codepipeline"].put_job_success_result.assert_called_once()
    clients["codepipeline"].put_job_failure_result.assert_not_called()


def test_blocks_when_over_threshold(monkeypatch, _boto3_stub):
    handler, clients = _boto3_stub
    monkeypatch.setattr(
        handler,
        "run_infracost",
        lambda **_: {"monthly_cost_delta": 120.0, "top_resources": [{"name": "nat", "monthly_cost": 90}]},
    )

    handler.lambda_handler(_event(), None)

    clients["codepipeline"].put_job_failure_result.assert_called_once()
    clients["codepipeline"].put_job_success_result.assert_not_called()


def test_error_fails_the_job(monkeypatch, _boto3_stub):
    handler, clients = _boto3_stub

    def boom(**_):
        raise RuntimeError("infracost exploded")

    monkeypatch.setattr(handler, "run_infracost", boom)

    handler.lambda_handler(_event(), None)

    clients["codepipeline"].put_job_failure_result.assert_called_once()


def test_parse_breakdown_extracts_top_drivers():
    from infracost_runner import _parse_breakdown

    raw = {
        "totalMonthlyCost": "150.5",
        "diffTotalMonthlyCost": "60.0",
        "projects": [
            {
                "breakdown": {
                    "resources": [
                        {"name": "aws_nat_gateway.main", "monthlyCost": "32.4"},
                        {"name": "aws_lb.app", "monthlyCost": "18.0"},
                        {"name": "free_thing", "monthlyCost": "0"},
                    ]
                }
            }
        ],
    }
    out = _parse_breakdown(raw)
    assert out["monthly_cost_delta"] == 60.0
    assert out["total_monthly_cost"] == 150.5
    assert out["top_resources"][0]["name"] == "aws_nat_gateway.main"
    assert len(out["top_resources"]) == 2  # zero-cost resource dropped

"""Unit tests for the security gate handler and scan summarisers."""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

GATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATE_DIR))


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """Stub boto3 + the anthropic SDK so no network/AWS calls happen."""
    import boto3

    clients = {"codepipeline": MagicMock(), "secretsmanager": MagicMock(), "s3": MagicMock()}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])

    # Provide a fake `anthropic` module before handler imports claude_summariser.
    if "anthropic" not in sys.modules:
        fake = types.ModuleType("anthropic")
        fake.Anthropic = MagicMock()
        sys.modules["anthropic"] = fake

    monkeypatch.setenv("SECRETS_ARN", "arn:aws:secretsmanager:region:acct:secret:x")

    # Pin this gate's dir at the front of sys.path and drop any cached gate
    # modules so we import THIS gate's handler (both gates use the name).
    monkeypatch.syspath_prepend(str(GATE_DIR))
    for mod in ("handler", "claude_summariser", "trivy_runner", "checkov_runner", "github_commenter"):
        sys.modules.pop(mod, None)
    import handler  # noqa: E402

    clients["secretsmanager"].get_secret_value.return_value = {
        "SecretString": json.dumps(
            {"ANTHROPIC_API_KEY": "sk-ant-test", "SLACK_WEBHOOK_URL": ""}
        )
    }
    yield handler, clients


def _event(user_params=None):
    return {
        "CodePipeline.job": {
            "id": "job-abc",
            "data": {
                "actionConfiguration": {
                    "configuration": {"UserParameters": json.dumps(user_params or {})}
                }
            },
        }
    }


def test_passes_with_no_high_or_critical(monkeypatch, _stubs):
    handler, clients = _stubs
    monkeypatch.setattr(handler, "run_trivy", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "run_checkov", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "summarise_findings", lambda **_: "all good")

    handler.lambda_handler(_event({"ecr_image_uri": "repo:tag"}), None)

    clients["codepipeline"].put_job_success_result.assert_called_once()
    clients["codepipeline"].put_job_failure_result.assert_not_called()


def test_blocks_on_critical(monkeypatch, _stubs):
    handler, clients = _stubs
    monkeypatch.setattr(handler, "run_trivy", lambda *_: {"HIGH": 1, "CRITICAL": 2})
    monkeypatch.setattr(handler, "run_checkov", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "summarise_findings", lambda **_: "bad")

    handler.lambda_handler(_event({"ecr_image_uri": "repo:tag"}), None)

    clients["codepipeline"].put_job_failure_result.assert_called_once()
    clients["codepipeline"].put_job_success_result.assert_not_called()


def test_error_fails_the_job(monkeypatch, _stubs):
    handler, clients = _stubs

    def boom(*_):
        raise RuntimeError("trivy exploded")

    monkeypatch.setattr(handler, "run_trivy", boom)
    handler.lambda_handler(_event({"ecr_image_uri": "repo:tag"}), None)

    clients["codepipeline"].put_job_failure_result.assert_called_once()


def test_direct_invoke_blocks_on_checkov(monkeypatch, _stubs):
    handler, _ = _stubs
    monkeypatch.setattr(handler, "run_trivy", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "run_checkov", lambda *_: {"HIGH": 3, "CRITICAL": 0})
    monkeypatch.setattr(handler, "summarise_findings", lambda **_: "checkov found issues")

    out = handler.lambda_handler({"ecr_image_uri": "repo:tag"}, None)
    assert out["gate_status"] == "failed"
    assert out["high"] == 3


def test_direct_invoke_passes_clean(monkeypatch, _stubs):
    handler, _ = _stubs
    monkeypatch.setattr(handler, "run_trivy", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "run_checkov", lambda *_: {"HIGH": 0, "CRITICAL": 0})
    monkeypatch.setattr(handler, "summarise_findings", lambda **_: "clean")

    out = handler.lambda_handler({"ecr_image_uri": "repo:tag"}, None)
    assert out["gate_status"] == "passed"


def test_summarise_trivy_counts_severities():
    from trivy_runner import summarise_trivy

    raw = {
        "Results": [
            {
                "Vulnerabilities": [
                    {"Severity": "CRITICAL", "VulnerabilityID": "CVE-1", "PkgName": "openssl"},
                    {"Severity": "HIGH", "VulnerabilityID": "CVE-2", "PkgName": "curl"},
                    {"Severity": "LOW", "VulnerabilityID": "CVE-3", "PkgName": "zlib"},
                ]
            }
        ]
    }
    out = summarise_trivy(raw)
    assert out["CRITICAL"] == 1
    assert out["HIGH"] == 1
    assert out["LOW"] == 1
    assert len(out["findings"]) == 2  # only HIGH/CRITICAL kept


def test_summarise_checkov_defaults_severity_to_high():
    from checkov_runner import summarise_checkov

    raw = {
        "results": {
            "failed_checks": [
                {"check_id": "CKV_AWS_1", "resource": "aws_s3_bucket.x", "check_name": "no encryption"},
            ]
        }
    }
    out = summarise_checkov(raw)
    assert out["HIGH"] == 1
    assert out["findings"][0]["check_id"] == "CKV_AWS_1"

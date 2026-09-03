"""
D-3: prior-report fetch and re-verify wiring (agents/qa/harness/main.py).

The runtime archives every run under a run-scoped key AND, when told, a
PR-stable `reports/<namespace>/latest/findings.json`. This is the READ half of
D-3: the harness fetches that alias before invoking, reconciles the previous
report against whatever this run returns, and hands the rows to the renderer --
so the second QA dispatch on a PR shows the `Prior findings re-verified` table.
The harness never writes; archiving stays under the runtime's role.

The reconciliation itself (verify_report) is exercised exhaustively in
agents/converge/tests; here the concern is the wiring -- fetch only when
configured, forward the namespace, render the block only when a prior exists.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(_HARNESS.parent / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "converge"))

import main as harness  # noqa: E402

_PASS = {"overall": "PASS", "pages_tested": 8, "findings": []}

# A plausible prior report: one HIGH blocker on /voyage. Shape matters only for
# verify_report, which reads page/severity/summary.
_PRIOR = {
    "overall": "FAIL",
    "pages_tested": 8,
    "findings": [
        {
            "id": "F-001",
            "severity": "HIGH",
            "page": "/voyage",
            "summary": "Voyage History tab crashes with TypeError on open",
            "evidence": "error boundary",
            "steps_to_reproduce": ["Open /voyage"],
            "expected": "history loads",
            "actual": "crash",
        }
    ],
}


def _args(tmp_path, **extra):
    argv = [
        "--runtime-arn", "arn:x",
        "--target-url", "https://t",
        "--comment-out", str(tmp_path / "comment.md"),
    ]
    for flag, value in extra.items():
        argv += [f"--{flag.replace('_', '-')}", value]
    return harness.build_parser().parse_args(argv)


class TestFetchPriorReport:
    def _mock_s3(self, monkeypatch, *, body=None, exc=None):
        client = MagicMock()
        if exc is not None:
            client.get_object.side_effect = exc
        else:
            stream = MagicMock()
            stream.read.return_value = body
            client.get_object.return_value = {"Body": stream}
        monkeypatch.setattr(harness.boto3, "client", lambda name, *a, **k: client)
        return client

    def test_fetches_the_pr_stable_alias(self, monkeypatch):
        client = self._mock_s3(monkeypatch, body=json.dumps(_PRIOR).encode())
        assert harness.fetch_prior_report("bucket", "pr-125") == _PRIOR
        assert client.get_object.call_args.kwargs["Key"] == "reports/pr-125/latest/findings.json"

    def test_a_missing_prior_is_none_not_an_error(self, monkeypatch):
        exc = ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        self._mock_s3(monkeypatch, exc=exc)
        assert harness.fetch_prior_report("bucket", "pr-125") is None

    def test_no_bucket_or_namespace_is_none(self):
        assert harness.fetch_prior_report("", "pr-1") is None
        assert harness.fetch_prior_report("bucket", "") is None

    def test_corrupt_body_is_none(self, monkeypatch):
        self._mock_s3(monkeypatch, body=b"<html>gateway</html>")
        assert harness.fetch_prior_report("bucket", "pr-125") is None


class TestReverifyWiring:
    def test_clean_rerun_with_a_prior_renders_a_board_not_a_fake_close(self, monkeypatch, tmp_path):
        """
        D-4: the prior HIGH is gone on a clean origin re-run with no fix signal,
        so the run renders the Reconciliation BOARD (the board replaces the plain
        re-verify table on a clean origin run). No fix leg intervened, so the row
        is NOT REPRODUCED -- never FIXED, never a bare absence, and the header is
        "Reconciliation", not "Final": a clean re-run closes nothing on its own.
        """
        args = _args(tmp_path, reports_bucket="bucket", report_namespace="pr-125")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_PASS))
        monkeypatch.setattr(harness, "fetch_prior_report", lambda *a, **k: dict(_PRIOR))
        # No fix leg ran, so there is no fix-verdict.json to fetch -- and the
        # fetch must not reach real S3 from a unit test.
        monkeypatch.setattr(harness, "_fetch_json", lambda *a, **k: None)

        assert harness.run(args) == 0
        comment = Path(tmp_path / "comment.md").read_text()
        assert "Reconciliation board" in comment
        assert "Final reconciliation board" not in comment
        assert "NOT REPRODUCED" in comment
        assert "Voyage History tab crashes" in comment
        assert "FIXED" not in comment

    def test_the_namespace_is_forwarded_to_the_runtime(self, monkeypatch, tmp_path):
        seen = {}

        def spy_invoke(runtime_arn, payload, **kw):
            seen["payload"] = payload
            return dict(_PASS)

        args = _args(tmp_path, reports_bucket="bucket", report_namespace="pr-125")
        monkeypatch.setattr(harness, "invoke", spy_invoke)
        monkeypatch.setattr(harness, "fetch_prior_report", lambda *a, **k: None)
        harness.run(args)
        assert seen["payload"]["report_namespace"] == "pr-125"

    def test_first_run_on_a_pr_has_no_block(self, monkeypatch, tmp_path):
        args = _args(tmp_path, reports_bucket="bucket", report_namespace="pr-125")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_PASS))
        monkeypatch.setattr(harness, "fetch_prior_report", lambda *a, **k: None)

        assert harness.run(args) == 0
        comment = Path(tmp_path / "comment.md").read_text()
        assert "Prior findings re-verified" not in comment

    def test_unconfigured_runs_never_touch_s3(self, monkeypatch, tmp_path):
        """Backward compatible: with no reports bucket configured, no S3 client
        is ever created and the payload carries no namespace -- the behaviour
        before D-3, unchanged."""
        seen = {}

        def spy_invoke(runtime_arn, payload, **kw):
            seen["payload"] = payload
            return dict(_PASS)

        def no_s3(*a, **k):
            raise AssertionError("boto3.client must not be called on an unconfigured run")

        args = _args(tmp_path)
        args.reports_bucket = ""  # in case the host env sets REPORTS_BUCKET
        monkeypatch.setattr(harness, "invoke", spy_invoke)
        monkeypatch.setattr(harness.boto3, "client", no_s3)
        assert harness.run(args) == 0
        assert "report_namespace" not in seen["payload"]

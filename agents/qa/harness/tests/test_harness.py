"""
Harness tests.

The harness is what actually posts to a PR, so what matters here is that it
never publishes something misleading: a failed run must not read as a pass, an
unknown price must not read as free, and evidence must arrive as something a
reviewer can click.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(_HARNESS.parent / "agent"))

import pricing  # noqa: E402
import report  # noqa: E402


PRICES = {
    "source": "test",
    "compute": {"vcpu_hour_usd": 0.0895, "gb_hour_usd": 0.00945},
    "models": {"priced-model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0}},
}


def _finding(**over):
    f = {
        "id": "F-001",
        "severity": "HIGH",
        "page": "/voyage",
        "summary": "Fuel chart renders blank",
        "evidence": "Chart container present, zero data points",
        "steps_to_reproduce": ["Log in", "Open /voyage"],
        "expected": "A fuel curve",
        "actual": "Empty chart",
        "suspected_source": None,
    }
    f.update(over)
    return f


def _findings(**over):
    d = {
        "overall": "FAIL",
        "pages_tested": 8,
        "session_seconds": 120,
        "findings": [_finding()],
        "cost": {
            "model_tokens": {"input": 1_000_000, "output": 200_000},
            "turns": 12,
            "model": "priced-model",
            "excludes": ["S3 storage"],
        },
    }
    d.update(over)
    return d


class TestPricing:
    def test_model_cost_is_per_million_tokens(self):
        assert pricing.model_cost_usd("priced-model", 1_000_000, 200_000, PRICES) == pytest.approx(2.0)

    def test_unknown_model_is_none_not_zero(self):
        """
        The whole point. A $0.00 reads as "free"; None reads as "we don't know",
        and only one of those is true.
        """
        assert pricing.model_cost_usd("mystery-model", 1_000, 1_000, PRICES) is None

    def test_compute_cost_scales_with_wall_clock(self):
        one = pricing.compute_cost_usd(3600, PRICES)
        half = pricing.compute_cost_usd(1800, PRICES)
        assert one == pytest.approx(half * 2)
        # 1 vCPU + 2 GB for an hour.
        assert one == pytest.approx(0.0895 + 2 * 0.00945)

    def test_summary_charges_runtime_and_browser_separately(self):
        """Two sessions run concurrently; folding them into one understates."""
        s = pricing.summarise(_findings(), PRICES)
        assert s["runtime_usd"] > 0 and s["browser_usd"] > 0
        assert s["estimated_total_usd"] == pytest.approx(
            s["model_usd"] + s["runtime_usd"] + s["browser_usd"]
        )

    def test_total_is_none_when_the_model_is_unpriced(self):
        f = _findings()
        f["cost"]["model"] = "mystery-model"
        s = pricing.summarise(f, PRICES)
        assert s["estimated_total_usd"] is None
        assert s["unpriced"] is True
        # Units are still exact -- that is what makes the None acceptable.
        assert s["input_tokens"] == 1_000_000
        assert s["session_seconds"] == 120

    def test_fmt_usd_says_unpriced_rather_than_a_number(self):
        assert pricing.fmt_usd(None) == "unpriced"
        assert pricing.fmt_usd(0.0004) == "$0.0004"
        assert pricing.fmt_usd(1.5) == "$1.50"

    def test_missing_price_file_degrades_instead_of_raising(self, tmp_path):
        p = pricing.load_prices(tmp_path / "nope.json")
        assert p["models"] == {}
        assert p["compute"]["vcpu_hour_usd"] > 0


class TestReport:
    def test_pass_headline(self):
        out = report.render({"overall": "PASS", "pages_tested": 8, "findings": [], "cost": {}, "session_seconds": 1})
        assert "✅" in out and "PASS" in out
        assert "No findings" in out

    def test_fail_headline_counts_blocking_only(self):
        f = _findings(findings=[_finding(), _finding(id="F-002", severity="LOW")])
        out = report.render(f)
        assert "❌" in out and "1 blocking" in out

    def test_findings_are_ordered_worst_first(self):
        f = _findings(
            findings=[
                _finding(id="F-001", severity="LOW"),
                _finding(id="F-002", severity="CRITICAL"),
                _finding(id="F-003", severity="MEDIUM"),
            ]
        )
        out = report.render(f)
        assert out.index("CRITICAL") < out.index("MEDIUM") < out.index("LOW")

    def test_presigned_screenshot_is_rendered_as_an_image(self):
        """A bare S3 key is unopenable -- the bucket blocks public access."""
        f = _findings(findings=[_finding(screenshot={"key": "screenshots/a.png", "url": "https://signed"})])
        assert "![F-001 evidence](https://signed)" in report.render(f)

    def test_screenshot_without_a_url_says_so(self):
        f = _findings(findings=[_finding(screenshot={"key": "screenshots/a.png"})])
        out = report.render(f)
        assert "no presigned URL" in out

    def test_failed_run_is_not_rendered_as_a_pass(self):
        out = report.render({"error": "schema_violation", "detail": "not JSON", "findings": []})
        assert "run failed" in out
        assert "PASS" not in out
        assert "not a passing run" in out

    def test_partial_run_is_labelled(self):
        f = _findings(incomplete=True, stop_reason="turn cap reached (40)")
        out = report.render(f)
        assert "partial run" in out and "turn cap" in out

    def test_unpriced_model_is_explained_not_hidden(self):
        f = _findings()
        f["cost"]["model"] = "mystery-model"
        out = report.render(f)
        assert "unpriced" in out
        assert "measured and exact" in out

    def test_comment_carries_a_marker_for_updating_in_place(self):
        assert report.render(_findings()).startswith(report.MARKER)

    def test_runner_minutes_reported_with_a_defensible_zero(self):
        out = report.render(_findings(), runner_minutes=7)
        assert "Runner minutes: 7" in out and "$0.00" in out


class TestExitCode:
    def test_clean_run(self):
        assert report.exit_code({"overall": "PASS", "findings": []}) == 0

    def test_blocking_findings(self):
        assert report.exit_code(_findings()) == 1

    def test_non_blocking_findings_do_not_fail(self):
        assert report.exit_code(_findings(findings=[_finding(severity="MEDIUM")])) == 0

    def test_failed_run_is_distinct_from_findings(self):
        """
        2, not 1. A broken harness must not read as a broken app -- they want
        different responses, and one code for both hides that.
        """
        assert report.exit_code({"error": "schema_violation", "findings": []}) == 2

    def test_partial_run_is_also_a_pipeline_failure(self):
        assert report.exit_code(_findings(incomplete=True)) == 2


class TestInvoke:
    def _harness(self, monkeypatch, body):
        import main as harness

        stream = MagicMock()
        stream.read.return_value = body
        client = MagicMock()
        client.invoke_agent_runtime.return_value = {"response": stream}
        monkeypatch.setattr(harness.boto3, "client", lambda *a, **k: client)
        return harness, client

    def test_session_id_meets_the_api_minimum_length(self):
        import main as harness

        assert len(harness.new_session_id()) >= 33

    def test_parses_a_json_response(self, monkeypatch):
        harness, _ = self._harness(monkeypatch, json.dumps({"overall": "PASS", "findings": []}).encode())
        assert harness.invoke("arn:x", {})["overall"] == "PASS"

    def test_non_json_becomes_a_failed_run_not_an_exception(self, monkeypatch):
        harness, _ = self._harness(monkeypatch, b"<html>502 Bad Gateway</html>")
        result = harness.invoke("arn:x", {})
        assert result["error"] == "invalid_runtime_response"
        assert result["findings"] == []

    def test_payload_is_sent_as_json_bytes(self, monkeypatch):
        harness, client = self._harness(monkeypatch, b'{"overall":"PASS","findings":[]}')
        harness.invoke("arn:x", {"target_url": "https://t"})
        sent = client.invoke_agent_runtime.call_args.kwargs
        assert json.loads(sent["payload"])["target_url"] == "https://t"
        assert sent["agentRuntimeArn"] == "arn:x"


class TestValidate:
    def test_revalidates_on_receipt(self):
        """
        The agent validated already. This is a trust boundary, and the process
        that publishes to a PR should not take the other side's word for it.
        """
        import main as harness

        bad = {"overall": "PASS", "pages_tested": 1, "findings": [{"id": "F-1", "severity": "HIGH"}]}
        assert harness.validate(bad)["error"] == "schema_violation"

    def test_passes_valid_findings_through(self):
        import main as harness

        good = {"overall": "PASS", "pages_tested": 8, "findings": []}
        assert harness.validate(good) is good

    def test_does_not_revalidate_an_error_envelope(self):
        import main as harness

        err = {"error": "bad_payload", "detail": "x", "findings": []}
        assert harness.validate(err) is err

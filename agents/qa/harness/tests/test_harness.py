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
        # Assert on the HEADLINE, not the whole body -- the explanatory hint
        # legitimately contains the word PASS while arguing against it.
        headline = [l for l in out.splitlines() if l.startswith("## ")][0]
        assert "run failed" in headline
        assert "PASS" not in headline
        assert "✅" not in out
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


class TestRuntimeFailures:
    """
    Regression tests for the first real run, which failed in a way that hid its
    own cause: the runtime returned 500, botocore raised, the harness died before
    writing a comment, and the workflow then failed three steps later on a
    missing file. The error the operator saw was `cat: comment.md: No such file`.

    Every failure mode must leave a renderable result behind.
    """

    def test_a_client_error_becomes_a_renderable_result(self, monkeypatch):
        import main as harness
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.invoke_agent_runtime.side_effect = ClientError(
            {"Error": {"Code": "RuntimeClientError", "Message": "Received error (500) from runtime"}},
            "InvokeAgentRuntime",
        )
        monkeypatch.setattr(harness.boto3, "client", lambda *a, **k: client)

        result = harness.invoke("arn:x", {})
        assert result["error"] == "runtime_unavailable"
        assert "500" in result["detail"]
        # The point: it renders, so the workflow always has something to publish.
        assert report.render(result)
        assert report.exit_code(result) == 2

    def test_a_connection_error_also_renders(self, monkeypatch):
        import main as harness
        from botocore.exceptions import EndpointConnectionError

        client = MagicMock()
        client.invoke_agent_runtime.side_effect = EndpointConnectionError(endpoint_url="https://x")
        monkeypatch.setattr(harness.boto3, "client", lambda *a, **k: client)
        assert harness.invoke("arn:x", {})["error"] == "runtime_unavailable"

    def test_error_report_points_at_the_runtime_logs(self):
        out = report.render({"error": "runtime_unavailable", "detail": "500", "findings": []})
        assert "CloudWatch" in out
        assert "not in this workflow" in out

    def test_every_error_code_has_a_hint(self):
        """A run that failed and explains nothing costs as much as one that points somewhere."""
        for code in ("runtime_unavailable", "schema_violation", "invalid_runtime_response", "bad_payload"):
            assert report._ERROR_HINTS.get(code)


class TestUnauthenticatedRun:
    """
    An unauthenticated run must never render as a pass. It is the one output
    where "no findings" actively misleads: the agent was looking at the login
    page and reporting the application healthy.
    """

    def test_it_renders_as_a_failure(self):
        out = report.render(
            {"error": "unauthenticated", "detail": "no session token", "findings": []}
        )
        # Assert on the HEADLINE, not the whole body -- the explanatory hint
        # legitimately contains the word PASS while arguing against it.
        headline = [l for l in out.splitlines() if l.startswith("## ")][0]
        assert "run failed" in headline
        assert "PASS" not in headline
        assert "✅" not in out

    def test_it_explains_why_the_findings_were_discarded(self):
        out = report.render({"error": "unauthenticated", "detail": "x", "findings": []})
        assert "login page" in out
        assert "indistinguishable from a healthy app" in out

    def test_it_is_a_pipeline_failure_not_a_findings_failure(self):
        assert report.exit_code({"error": "unauthenticated", "findings": []}) == 2

def _agent_default(name: str):
    """
    Read a constant out of agent.py without importing it.

    agent.py pulls in bedrock_agentcore (and pydantic and starlette beneath it),
    which CI deliberately does not install for the harness tests. Reading the
    literal keeps the cross-file check without the dependency -- and reading it,
    rather than copying it here, is the point: a duplicated constant drifts, and
    catching drift is what the check is for.
    """
    import ast

    source = _HARNESS.parent / "agent/agent.py"
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {source}")


class TestShippedPriceTable:
    """
    The SHIPPED prices.json, not a fixture.

    An empty `models` table is a defensible degraded state -- and it was the
    state this shipped in, which meant every run reported "unpriced" and no
    total. Phase 1's exit criterion is a PR comment carrying the cost of the
    run, so a table that prices nothing does not meet it. These tests make the
    table's completeness a property of the build rather than of somebody
    remembering.
    """

    @staticmethod
    def _table():
        return json.loads((_HARNESS / "prices.json").read_text())

    def test_the_default_model_is_priced(self):
        """
        The rung that runs unless someone overrides it. If only the expensive
        rung were priced, every scheduled run would still report "unpriced".
        """
        table = self._table()
        assert _agent_default("DEFAULT_MODEL") in table["models"]

    def test_every_entry_is_dated_and_sourced(self):
        """
        A price with no date cannot be audited and cannot be known to be stale,
        which is the failure mode PRICING.md exists to prevent.
        """
        for model, entry in self._table()["models"].items():
            assert entry["input_usd_per_mtok"] > 0, model
            assert entry["output_usd_per_mtok"] > 0, model
            assert entry.get("read_on"), f"{model} has no read_on date"
            assert entry.get("source"), f"{model} does not say where the price came from"

    def test_every_granted_rung_is_priced(self):
        """
        Cross-file invariant. Terraform enumerates the inference profiles the
        agent's IAM policy allows it to invoke; a rung that can be invoked but
        not priced reports "unpriced" the first time anyone uses it, which is
        discovered on a PR rather than here.
        """
        import re

        tf = (_HARNESS.parents[2] / "infra/modules/qa_agent/variables.tf").read_text()
        block = re.search(
            r'variable "model_profile_ids".*?default\s*=\s*\[(.*?)\]', tf, re.DOTALL
        )
        assert block, "could not find model_profile_ids in variables.tf"
        granted = set(re.findall(r'"([^"]+)"', block.group(1)))
        assert granted, "no model profile ids parsed"
        assert granted <= set(self._table()["models"]), (
            f"granted but unpriced: {granted - set(self._table()['models'])}"
        )

    def test_a_priced_run_reports_a_total_and_says_nothing_is_unpriced(self):
        """The end state B2 was blocking: a comment with real money in it."""
        findings = _findings()
        findings["cost"]["model"] = _agent_default("DEFAULT_MODEL")
        cost = pricing.summarise(findings, self._table())

        assert cost["unpriced"] is False
        assert cost["estimated_total_usd"] is not None
        comment = report.render(findings)
        assert "unpriced" not in comment
        assert "$" in comment

class TestCachePricing:
    """
    Input arrives on three meters once caching is on, and they differ by more
    than 10x. Pricing cache reads at the full input rate overstates a cached run
    by roughly that much; treating them as free understates it. Either way the
    number in the PR comment is wrong, which is the one thing this file exists
    to prevent.
    """

    _WITH_CACHE = {
        "source": "test",
        "compute": {"vcpu_hour_usd": 0.0895, "gb_hour_usd": 0.00945},
        "models": {
            "m": {
                "input_usd_per_mtok": 1.0,
                "output_usd_per_mtok": 5.0,
                "cache_read_usd_per_mtok": 0.10,
                "cache_write_usd_per_mtok": 1.25,
            }
        },
    }

    def test_each_meter_is_charged_at_its_own_rate(self):
        cost = pricing.model_cost_usd(
            "m", 1_000_000, 1_000_000, self._WITH_CACHE,
            cache_read=1_000_000, cache_write=1_000_000,
        )
        assert cost == pytest.approx(1.0 + 5.0 + 0.10 + 1.25)

    def test_cache_reads_are_not_free(self):
        """A silent zero is the failure mode this module is built against."""
        priced = pricing.model_cost_usd("m", 0, 0, self._WITH_CACHE, cache_read=1_000_000)
        assert priced > 0

    def test_missing_cache_rates_fall_back_to_documented_multipliers(self):
        """
        An entry with base rates but no cache rates must not price cached tokens
        at zero. 0.1x read / 1.25x write, the multipliers Sonnet 4.6's published
        rates confirm.
        """
        table = {"models": {"m": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0}}}
        cost = pricing.model_cost_usd(
            "m", 0, 0, table, cache_read=1_000_000, cache_write=1_000_000
        )
        assert cost == pytest.approx(0.1 + 1.25)

    def test_an_explicit_rate_beats_the_multiplier(self):
        table = {"models": {"m": {
            "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0,
            "cache_read_usd_per_mtok": 0.42,
        }}}
        assert pricing.model_cost_usd("m", 0, 0, table, cache_read=1_000_000) == pytest.approx(0.42)

    def test_caching_makes_a_run_materially_cheaper(self):
        """
        The point of the whole exercise, as arithmetic: the same tokens, cached,
        against the same tokens uncached.
        """
        uncached = pricing.model_cost_usd("m", 900_000, 10_000, self._WITH_CACHE)
        cached = pricing.model_cost_usd(
            "m", 50_000, 10_000, self._WITH_CACHE, cache_read=800_000, cache_write=50_000
        )
        assert cached < uncached / 3

    def test_the_hit_rate_is_reported(self):
        findings = _findings()
        findings["cost"]["model"] = "m"
        findings["cost"]["model_tokens"] = {
            "input": 100_000, "output": 5_000,
            "cache_read": 800_000, "cache_write": 100_000,
        }
        cost = pricing.summarise(findings, self._WITH_CACHE)
        assert cost["total_input_tokens"] == 1_000_000
        assert cost["cache_hit_rate"] == pytest.approx(0.8)
        assert "80%" in report.render(findings)

    def test_a_cache_that_never_matched_is_called_out(self):
        """
        Writes with no reads across a multi-turn run means the prefix is being
        invalidated every turn. It costs several times what it should and is
        otherwise completely invisible.
        """
        findings = _findings()
        findings["cost"]["model"] = "m"
        findings["cost"]["model_tokens"] = {
            "input": 500_000, "output": 5_000, "cache_read": 0, "cache_write": 500_000,
        }
        comment = report.render(findings)
        assert "0% hit rate" in comment

    def test_an_uncached_run_says_nothing_about_caching(self):
        comment = report.render(_findings())
        assert "Prompt cache: not used" in comment

class TestTargetUrlIsNotPublished:
    """
    PLAN.md 1e: the tunnel is an unauthenticated public URL, and the plan says
    plainly not to write it anywhere durable -- "not into the PR comment, not
    into the findings JSON, not into logs." The comment used to lead with it, on
    a public repo.
    """

    def test_the_comment_does_not_carry_the_target_url(self):
        comment = report.render(_findings())
        assert "trycloudflare" not in comment
        assert "https://" not in comment.replace("https://example", "")

    def test_the_route_count_survives(self):
        """Removing the URL must not remove the coverage figure with it."""
        assert "8 routes tested" in report.render(_findings())


class TestReachableKnobs:
    """
    The agent accepted these and the CLI could not send them, which made one of
    its own error messages unactionable: a truncated run tells the reader to
    raise max_tokens_per_call through a flag that did not exist.
    """

    def test_max_tokens_per_call_reaches_the_payload(self):
        import main as harness

        args = harness.build_parser().parse_args(
            ["--runtime-arn", "a", "--target-url", "u", "--max-tokens-per-call", "8192"]
        )
        assert args.max_tokens_per_call == 8192

    def test_unset_knobs_are_not_sent_at_all(self):
        """
        An unset flag must stay absent from the payload so the AGENT's default
        applies. Sending None would override a derived default with nothing.
        """
        import main as harness

        args = harness.build_parser().parse_args(["--runtime-arn", "a", "--target-url", "u"])
        assert args.max_tokens_per_call is None and args.presign_expires is None

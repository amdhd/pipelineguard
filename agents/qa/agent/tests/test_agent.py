"""
Agent entrypoint tests.

agent.py imports bedrock_agentcore at module level, which CI does not install --
it pulls pydantic and starlette for no test benefit. The repo already solves this
exact problem in gates/security_gate/tests/test_handler.py, which stubs the
`anthropic` module before importing the handler. Same approach here.

What this covers is the logic that decides how a run ENDS: budget exhaustion,
whether model output is accepted, and whether a failure is reported as structured
data or escapes as a crash. The browser path is covered against a fake in
test_browser_tools.py; the tool-use loop itself needs a live model and is
exercised by a real invoke instead.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _stub_agentcore(monkeypatch):
    """Stub bedrock_agentcore + boto3 so importing agent.py needs neither."""
    if "bedrock_agentcore" not in sys.modules:
        pkg = types.ModuleType("bedrock_agentcore")

        class _App:
            def entrypoint(self, func):
                return func

            def run(self, *a, **k):  # pragma: no cover
                pass

        pkg.BedrockAgentCoreApp = _App
        sys.modules["bedrock_agentcore"] = pkg

        tools = types.ModuleType("bedrock_agentcore.tools")
        browser = types.ModuleType("bedrock_agentcore.tools.browser_client")
        browser.browser_session = MagicMock()
        sys.modules["bedrock_agentcore.tools"] = tools
        sys.modules["bedrock_agentcore.tools.browser_client"] = browser

    import boto3

    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())


@pytest.fixture
def agent(_stub_agentcore):
    import agent as agent_module

    return agent_module


class TestBudget:
    """
    The three independent stop conditions. Wall-clock matters on its own because
    browser memory bills for session duration including idle -- a wedged run
    costs money while generating no tokens, so a token cap alone cannot bound it.
    """

    def test_fresh_budget_is_not_exhausted(self, agent):
        b = agent.Budget(max_turns=10, token_budget=1000, deadline_seconds=60)
        assert b.exhausted() is None

    def test_turn_cap(self, agent):
        b = agent.Budget(max_turns=2, token_budget=10**9, deadline_seconds=600)
        b.record({"inputTokens": 1, "outputTokens": 1})
        assert b.exhausted() is None
        b.record({"inputTokens": 1, "outputTokens": 1})
        assert "turn cap" in b.exhausted()

    def test_token_budget_counts_input_and_output(self, agent):
        b = agent.Budget(max_turns=100, token_budget=100, deadline_seconds=600)
        b.record({"inputTokens": 60, "outputTokens": 0})
        assert b.exhausted() is None
        b.record({"inputTokens": 0, "outputTokens": 41})
        assert "token budget" in b.exhausted()

    def test_wall_clock_deadline(self, agent):
        b = agent.Budget(max_turns=100, token_budget=10**9, deadline_seconds=0)
        assert "wall-clock" in b.exhausted()

    def test_missing_usage_fields_do_not_crash(self, agent):
        """A Converse response without a usage block must not kill the run."""
        b = agent.Budget(max_turns=10, token_budget=1000, deadline_seconds=60)
        b.record({})
        assert b.turns == 1
        assert b.total_tokens == 0


class TestExtractJson:
    def test_bare_json(self, agent):
        assert agent._extract_json('{"overall": "PASS"}') == {"overall": "PASS"}

    def test_fenced_json(self, agent):
        """
        Not defensive padding: the model demonstrably wraps its output in a
        ```json fence, confirmed against live Bedrock. Without this every real
        run would be rejected as a schema violation.
        """
        fenced = '```json\n{"overall": "PASS", "findings": []}\n```'
        assert agent._extract_json(fenced)["overall"] == "PASS"

    def test_fence_without_a_language_tag(self, agent):
        assert agent._extract_json('```\n{"overall": "FAIL"}\n```')["overall"] == "FAIL"

    def test_prose_is_rejected(self, agent):
        """
        Prime directive 6. The reference implementation parsed narration into
        HIGH-severity findings reading "Let me verify this further"; refusing to
        salvage prose is what prevents that.
        """
        import schema

        with pytest.raises(schema.SchemaError, match="not valid JSON"):
            agent._extract_json("Let me verify this further before reporting.")

    def test_fenced_json_after_narration_is_now_ACCEPTED(self, agent):
        """
        REVERSED, by a real run. This previously asserted rejection, on the
        principle that salvaging JSON from prose hides a prompt bug.

        The model narrated for a paragraph and then emitted a complete,
        schema-valid findings object in a ```json fence. The findings were
        correct; the parser threw them away.

        A fence is an explicit self-delimiting payload, not prose, so reading it
        is not the "inference" the directive guards against. Narration is still
        discarded and can still never become a finding -- only a schema-valid
        object survives.
        """
        real = (
            "I'm being redirected again. This is expected behavior and not a "
            "finding.\n\nLet me compile the results.\n\n"
            '```json\n{"overall": "PASS", "pages_tested": 3, "findings": []}\n```'
        )
        assert agent._extract_json(real)["pages_tested"] == 3

    def test_the_last_fence_wins(self, agent):
        """An earlier fence may be the model quoting the schema back to itself."""
        text = (
            'Here is the shape:\n```json\n{"overall": "EXAMPLE"}\n```\n'
            'And my report:\n```json\n{"overall": "PASS", "pages_tested": 1, "findings": []}\n```'
        )
        assert agent._extract_json(text)["overall"] == "PASS"

    def test_unfenced_prose_is_still_rejected(self, agent):
        """No fence, no salvage. This half of the directive stands."""
        import schema

        with pytest.raises(schema.SchemaError, match="not valid JSON"):
            agent._extract_json("Let me verify this further before reporting.")


class TestScreenshotSink:
    def test_key_is_namespaced_by_session(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "some-bucket")
        key = agent._screenshot_sink("run-42")("blank-chart", b"png-bytes")
        assert key == "screenshots/run-42/blank-chart.png"

    def test_survives_an_unset_bucket(self, agent, monkeypatch):
        """Missing config degrades to an unpersisted key, never a crashed run."""
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "")
        assert agent._screenshot_sink("run-1")("x", b"png") == "screenshots/run-1/x.png"


class TestPresign:
    def test_no_bucket_yields_no_urls(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "")
        assert agent._presign(["screenshots/a.png"], 3600) == {}

    def test_a_failing_presign_does_not_fail_the_run(self, agent, monkeypatch):
        """Evidence links are a convenience; findings are the product."""
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "b")
        client = MagicMock()
        client.generate_presigned_url.side_effect = RuntimeError("kms denied")
        monkeypatch.setattr("boto3.client", lambda *a, **k: client)
        assert agent._presign(["screenshots/a.png"], 3600) == {}


class TestInvoke:
    """
    Failures must come back as STRUCTURED data. The harness has to distinguish
    "the agent ran and produced nonsense" from "the agent crashed" -- those want
    different responses on a PR, and an exception escaping the entrypoint
    collapses them into one opaque 500.
    """

    def test_missing_field_reports_bad_payload(self, agent):
        result = agent.invoke({"session_id": "x"})
        assert result["error"] == "bad_payload"
        assert "target_url" in result["detail"]
        assert result["findings"] == []

    def test_schema_violation_is_reported_not_raised(self, agent, monkeypatch):
        import schema

        def boom(_payload):
            raise schema.SchemaError("model output is not valid JSON")

        monkeypatch.setattr(agent, "run_qa", boom)
        result = agent.invoke({"target_url": "https://x", "email": "a", "password": "b"})
        assert result["error"] == "schema_violation"
        assert result["findings"] == []

    def test_findings_pass_through_untouched(self, agent, monkeypatch):
        payload = {"overall": "PASS", "pages_tested": 8, "findings": []}
        monkeypatch.setattr(agent, "run_qa", lambda _p: payload)
        assert agent.invoke({"target_url": "u", "email": "e", "password": "p"}) is payload


def test_emit_metrics_never_fails_the_run(agent, monkeypatch):
    """Telemetry is not worth losing a completed run over."""
    client = MagicMock()
    client.put_metric_data.side_effect = RuntimeError("throttled")
    monkeypatch.setattr("boto3.client", lambda *a, **k: client)
    budget = agent.Budget(max_turns=1, token_budget=1, deadline_seconds=1)
    agent._emit_metrics(budget, "some-model")  # must not raise


def test_default_model_matches_the_cheap_benchmark_rung(agent):
    """
    PLAN.md 1d: the default is the cheap rung, and the quality rung is an
    explicit opt-in. If this flips, every scheduled run silently gets dearer.
    """
    assert "haiku" in agent.DEFAULT_MODEL


def test_token_budget_is_cumulative_not_per_call(agent):
    """
    Guards a bug that existed here: the cumulative budget was conflated with the
    per-call max_tokens, so a 4096 "budget" halted every run after one turn.
    """
    assert agent.DEFAULT_TOKEN_BUDGET > agent.DEFAULT_MAX_TOKENS_PER_CALL * 10


class TestSchemaViolationDiagnostics:
    """
    The second real run failed with "Expecting value: line 1 column 1 (char 0)"
    and nothing else. That message is identical whether the model narrated,
    returned nothing, or was cut off mid-JSON -- and those need three different
    fixes. A failure you cannot diagnose is a failure you cannot tune the rubric
    against, which is the whole Phase 1 exit criterion.
    """

    def test_empty_output_says_so_explicitly(self, agent):
        import schema

        with pytest.raises(schema.SchemaError, match="NO TEXT"):
            agent._extract_json("")

    def test_whitespace_only_counts_as_empty(self, agent):
        import schema

        with pytest.raises(schema.SchemaError, match="NO TEXT"):
            agent._extract_json("   \n  ")

    def test_prose_carries_a_snippet_of_what_arrived(self, agent):
        import schema

        with pytest.raises(schema.SchemaError, match="Let me verify"):
            agent._extract_json("Let me verify this further before reporting.")

    def test_snippet_is_bounded(self, agent):
        """A whole essay in an error message helps nobody."""
        import schema

        try:
            agent._extract_json("x" * 5000)
        except schema.SchemaError as e:
            assert len(str(e)) < 600
        else:
            raise AssertionError("should have raised")


class TestArchiving:
    """
    A completed run whose findings cannot be retrieved is barely a run. The third
    real run passed, and the results existed only in a GitHub step summary --
    nothing in S3, no artifact. It also blocks Phase 3, whose convergence check
    compares finding sets across rounds.
    """

    def test_findings_are_written_under_the_reports_prefix(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "bucket")
        client = MagicMock()
        monkeypatch.setattr("boto3.client", lambda *a, **k: client)

        key = agent._archive("run-7", {"overall": "PASS", "findings": []})
        assert key == "reports/run-7/findings.json"
        kwargs = client.put_object.call_args.kwargs
        assert kwargs["Bucket"] == "bucket"
        # Must match the execution role's s3:PutObject scope, which is limited to
        # screenshots/* and reports/*.
        assert kwargs["Key"].startswith("reports/")
        assert kwargs["ContentType"] == "application/json"

    def test_archiving_failure_never_fails_the_run(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "bucket")
        client = MagicMock()
        client.put_object.side_effect = RuntimeError("denied")
        monkeypatch.setattr("boto3.client", lambda *a, **k: client)
        assert agent._archive("run-7", {}) is None

    def test_no_bucket_configured_is_not_an_error(self, agent, monkeypatch):
        monkeypatch.setattr(agent, "REPORTS_BUCKET", "")
        assert agent._archive("run-7", {}) is None

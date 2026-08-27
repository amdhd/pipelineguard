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


class TestDerivedBudgets:
    """
    The defaults have to describe a run that can FINISH.

    They did not. A flat 200_000-token budget, 40 turns and 8 routes were chosen
    independently, and context grows quadratically because every turn re-sends
    the whole history. Solving for 200k gives ~15 turns against the ~25 a login
    plus eight routes needs -- so the token cap fired mid-sweep on a default run
    and the turn cap was unreachable. Deriving one from the other is what stops
    three individually-reasonable numbers describing an impossible run.
    """

    def test_turns_and_budget_rise_with_routes(self, agent):
        assert agent.turns_for(2) < agent.turns_for(4) < agent.turns_for(8)
        assert agent.token_budget_for(2) < agent.token_budget_for(4) < agent.token_budget_for(8)

    def test_budget_covers_the_quadratic_growth_it_models(self, agent):
        """
        Simulate the run the defaults describe and assert it does not run out.
        This is the arithmetic the old defaults failed.
        """
        for routes in (2, 4, 8):
            turns = agent.turns_for(routes)
            budget = agent.token_budget_for(routes)
            # Cumulative input across `turns` turns, at the model this file uses.
            spent = sum(
                agent._BASE_TOKENS + agent._TOKENS_PER_TURN * i for i in range(turns)
            )
            assert spent <= budget, f"{routes} routes: needs {spent}, budgeted {budget}"

    def test_the_old_flat_budget_would_not_have_covered_the_default_sweep(self, agent):
        """The regression, stated as arithmetic rather than as a story."""
        turns = agent.turns_for(agent.DEFAULT_MAX_ROUTES)
        spent = sum(agent._BASE_TOKENS + agent._TOKENS_PER_TURN * i for i in range(turns))
        assert spent > 200_000
        assert spent <= agent.DEFAULT_TOKEN_BUDGET

    def test_defaults_are_derived_from_the_route_cap(self, agent):
        assert agent.DEFAULT_MAX_TURNS == agent.turns_for(agent.DEFAULT_MAX_ROUTES)
        assert agent.DEFAULT_TOKEN_BUDGET == agent.token_budget_for(agent.DEFAULT_MAX_ROUTES)


class TestReportReserve:
    """
    Stopping EARLY is what makes reporting possible: the findings exist only in
    the model's final message, so a budget with nothing left cannot ask for one.
    """

    def test_no_reserve_requested_means_no_reserve_held(self, agent):
        """The caps keep their plain meaning when nothing asked for a reserve."""
        b = agent.Budget(max_turns=100, token_budget=100, deadline_seconds=600)
        b.record({"inputTokens": 60, "outputTokens": 0})
        assert b.reserve() == 0
        assert b.exhausted() is None

    def test_reserve_stops_the_loop_with_room_for_one_more_call(self, agent):
        b = agent.Budget(
            max_turns=100, token_budget=10_000, deadline_seconds=600, report_tokens=1_000
        )
        b.record({"inputTokens": 4_000, "outputTokens": 500})
        # 4_500 spent, and one more call would cost ~4_000 + 1_000. That lands
        # under 10_000, so there is still room to continue.
        assert b.exhausted() is None
        b.record({"inputTokens": 4_500, "outputTokens": 500})
        # 9_500 spent; another call cannot fit. Stop now, while the reserve is
        # still unspent.
        assert "token budget" in b.exhausted()
        assert b.total_tokens < b.token_budget

    def test_reserve_tracks_the_growing_context(self, agent):
        b = agent.Budget(
            max_turns=100, token_budget=10**9, deadline_seconds=600, report_tokens=1_000
        )
        b.record({"inputTokens": 2_000, "outputTokens": 10})
        assert b.reserve() == 3_000
        b.record({"inputTokens": 9_000, "outputTokens": 10})
        assert b.reserve() == 10_000

    def test_deadline_reserves_seconds_too(self, agent):
        """
        The salvage call needs wall-clock as well as tokens. A deadline with no
        slack leaves no time to make it.
        """
        b = agent.Budget(
            max_turns=100, token_budget=10**9, deadline_seconds=30, report_seconds=60
        )
        assert "wall-clock" in b.exhausted()


class TestFinalReport:
    """
    A truncated run must report WHAT IT FOUND.

    It used to return `"findings": []` with a label, so a run that stopped after
    finding four defects reported none of them -- and since the token budget was
    the cap most likely to bind, that was the common path, not the rare one.
    """

    @staticmethod
    def _bedrock(text=None, stop_reason="end_turn", error=None):
        client = MagicMock()
        if error is not None:
            client.converse.side_effect = error
            return client
        client.converse.return_value = {
            "output": {"message": {"role": "assistant", "content": [{"text": text or ""}]}},
            "stopReason": stop_reason,
            "usage": {"inputTokens": 500, "outputTokens": 100},
        }
        return client

    @staticmethod
    def _messages():
        """A history that ends the way the loop always ends: on tool results."""
        return [
            {"role": "user", "content": [{"text": "go"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "1", "name": "navigate", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "1", "content": [{"json": {}}]}}]},
        ]

    _REPORT = (
        "I have run out of budget. Here is what I found.\n\n"
        '```json\n{"overall": "FAIL", "pages_tested": 3, "findings": ['
        '{"id": "F-001", "severity": "HIGH", "page": "/voyage", "summary": "blank chart",'
        ' "evidence": "no data points", "steps_to_reproduce": ["log in"],'
        ' "expected": "a curve", "actual": "empty", "suspected_source": null}]}\n```'
    )

    def test_findings_survive_budget_exhaustion(self, agent):
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        out = agent._final_report(
            self._bedrock(self._REPORT), "m", "sys", self._messages(), b, 4096, "token budget reached"
        )
        assert out is not None
        assert len(out["findings"]) == 1
        assert out["findings"][0]["id"] == "F-001"

    def test_the_nudge_does_not_create_two_user_turns_in_a_row(self, agent):
        """
        Converse requires alternating roles, and the loop always stops on a user
        message of tool results. Appending a second user message would be
        rejected outright -- turning the salvage into another way to lose the run.
        """
        messages = self._messages()
        client = self._bedrock(self._REPORT)
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        agent._final_report(client, "m", "sys", messages, b, 4096, "deadline")

        roles = [m["role"] for m in messages]
        assert not any(a == b_ == "user" for a, b_ in zip(roles, roles[1:]))
        assert any("STOP" in blk.get("text", "") for blk in messages[-1]["content"])

    def test_the_salvage_call_is_charged_to_the_budget(self, agent):
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        agent._final_report(
            self._bedrock(self._REPORT), "m", "sys", self._messages(), b, 4096, "deadline"
        )
        assert b.input_tokens == 500 and b.output_tokens == 100

    def test_another_tool_call_salvages_nothing(self, agent):
        """
        If the model keeps exploring rather than reporting, say so. Presenting an
        empty findings list as a result would be the same lie in a new place.
        """
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        out = agent._final_report(
            self._bedrock("...", stop_reason="tool_use"), "m", "sys", self._messages(), b, 4096, "deadline"
        )
        assert out is None

    def test_narration_without_json_salvages_nothing(self, agent):
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        out = agent._final_report(
            self._bedrock("Let me check a few more things first."),
            "m", "sys", self._messages(), b, 4096, "deadline",
        )
        assert out is None

    def test_a_failing_model_call_salvages_nothing_rather_than_raising(self, agent):
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")
        b = agent.Budget(max_turns=10, token_budget=10**9, deadline_seconds=600)
        out = agent._final_report(
            self._bedrock(error=err), "m", "sys", self._messages(), b, 4096, "deadline"
        )
        assert out is None


class TestPromptCaching:
    """
    This loop re-sends its entire history on every call, so without a cache
    checkpoint the same tokens are bought again at full price up to thirty times
    in one run. These pin the two things that make caching work: exactly one
    rolling checkpoint, and accounting that knows a cached token is still a token.
    """

    def test_a_checkpoint_is_placed_at_the_end(self, agent):
        messages = [{"role": "user", "content": [{"text": "go"}]}]
        agent._place_cache_point(messages)
        assert messages[-1]["content"][-1] == {"cachePoint": {"type": "default"}}

    def test_only_one_checkpoint_survives(self, agent):
        """
        Bedrock allows four per request and looks back ~20 content blocks for the
        longest match, so old markers buy nothing and spend the allowance.
        """
        messages = [
            {"role": "user", "content": [{"text": "a"}]},
            {"role": "assistant", "content": [{"text": "b"}]},
        ]
        for _ in range(5):
            agent._place_cache_point(messages)
            messages.append({"role": "user", "content": [{"text": "more"}]})
        agent._place_cache_point(messages)

        points = sum(
            1 for m in messages for b in m["content"] if "cachePoint" in b
        )
        assert points == 1

    def test_the_checkpoint_moves_to_the_new_tail(self, agent):
        messages = [{"role": "user", "content": [{"text": "a"}]}]
        agent._place_cache_point(messages)
        messages.append({"role": "assistant", "content": [{"text": "b"}]})
        messages.append({"role": "user", "content": [{"toolResult": {}}]})
        agent._place_cache_point(messages)

        assert "cachePoint" not in messages[0]["content"][-1]
        assert "cachePoint" in messages[-1]["content"][-1]

    def test_it_does_not_disturb_the_content_it_marks(self, agent):
        """The prefix must stay byte-identical or every cache read misses."""
        messages = [{"role": "user", "content": [{"text": "the prompt"}]}]
        agent._place_cache_point(messages)
        agent._place_cache_point(messages)
        assert messages[0]["content"][0] == {"text": "the prompt"}

    def test_empty_history_is_survivable(self, agent):
        assert agent._place_cache_point([]) == []


class TestCachedTokenAccounting:
    """
    With caching on, Bedrock reports `inputTokens` as the NON-cached portion
    only. Summing it alone -- which is what the budget did before caching --
    under-counts a cached turn by an order of magnitude, so the token cap stops
    binding and the run drifts until the wall-clock deadline catches it, with
    the browser meter collecting the difference.
    """

    def test_cached_tokens_count_towards_the_budget(self, agent):
        b = agent.Budget(max_turns=100, token_budget=10_000, deadline_seconds=600)
        b.record({"inputTokens": 200, "cacheReadInputTokens": 9_000,
                  "cacheWriteInputTokens": 500, "outputTokens": 300})
        assert b.total_tokens == 10_000
        assert "token budget" in b.exhausted()

    def test_the_meters_stay_separate_for_pricing(self, agent):
        b = agent.Budget(max_turns=100, token_budget=10**9, deadline_seconds=600)
        b.record({"inputTokens": 200, "cacheReadInputTokens": 9_000,
                  "cacheWriteInputTokens": 500, "outputTokens": 300})
        assert (b.input_tokens, b.cache_read_tokens, b.cache_write_tokens) == (200, 9_000, 500)

    def test_the_reserve_sizes_itself_on_total_input_not_billed_input(self, agent):
        """
        The next call re-sends the whole history whether or not it is cached, so
        a reserve computed from the uncached slice alone would be far too small
        and the salvage call would not fit.
        """
        b = agent.Budget(max_turns=100, token_budget=10**9, deadline_seconds=600,
                         report_tokens=1_000)
        b.record({"inputTokens": 200, "cacheReadInputTokens": 9_000,
                  "cacheWriteInputTokens": 500, "outputTokens": 300})
        assert b.reserve() == 9_700 + 1_000

    def test_a_run_without_caching_is_unaffected(self, agent):
        """Absent cache fields must behave exactly as before."""
        b = agent.Budget(max_turns=100, token_budget=10**9, deadline_seconds=600)
        b.record({"inputTokens": 100, "outputTokens": 50})
        assert b.total_tokens == 150
        assert b.cache_read_tokens == 0 and b.cache_write_tokens == 0

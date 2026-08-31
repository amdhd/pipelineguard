"""
Model-layer tests. No network: `converse` is stubbed, because what is being
tested is the harness's handling of what comes back, not Bedrock.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fix_schema  # noqa: E402
import model as fix_model  # noqa: E402
import prompt as fix_prompt  # noqa: E402


def _response(text, *, usage=None, stop="end_turn"):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": usage or {"inputTokens": 100, "outputTokens": 20},
        "stopReason": stop,
    }


class _Bedrock:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


VALID = {
    "edits": [
        {
            "finding_id": "F-001",
            "file": "frontend/src/App.tsx",
            "old_string": "a",
            "new_string": "b",
            "rationale": "because",
        }
    ],
    "skipped": [],
}


class TestSchema:
    def test_a_valid_response_passes(self):
        assert fix_schema.validate(json.loads(json.dumps(VALID)))

    @pytest.mark.parametrize("key", ["edits", "skipped"])
    def test_a_missing_top_level_list_is_rejected(self, key):
        payload = json.loads(json.dumps(VALID))
        del payload[key]
        with pytest.raises(fix_schema.FixSchemaError, match=key):
            fix_schema.validate(payload)

    @pytest.mark.parametrize("key", ["finding_id", "file", "old_string", "new_string", "rationale"])
    def test_a_missing_edit_key_is_named(self, key):
        payload = json.loads(json.dumps(VALID))
        del payload["edits"][0][key]
        with pytest.raises(fix_schema.FixSchemaError, match=key):
            fix_schema.validate(payload)

    def test_an_empty_old_string_is_rejected_at_the_schema_too(self):
        """
        Also checked in edits.py. This one catches a PROMPT failure -- the model
        was told explicitly -- while that one protects the filesystem.
        """
        payload = json.loads(json.dumps(VALID))
        payload["edits"][0]["old_string"] = ""
        with pytest.raises(fix_schema.FixSchemaError, match="old_string is empty"):
            fix_schema.validate(payload)

    def test_a_skip_without_a_reason_is_rejected(self):
        with pytest.raises(fix_schema.FixSchemaError, match="reason"):
            fix_schema.validate({"edits": [], "skipped": [{"finding_id": "F-1"}]})

    def test_both_lists_empty_is_valid(self):
        assert fix_schema.validate({"edits": [], "skipped": []})


class TestExtraction:
    def test_a_fenced_block_after_narration_is_read(self):
        """
        The QA agent lost a correct report to this exact shape. Narration is
        discarded; the fence is an explicit payload, so reading it is not
        inference.
        """
        text = "Let me look at this.\n\n```json\n" + json.dumps(VALID) + "\n```"
        assert fix_model.extract_json(text)["edits"][0]["file"] == "frontend/src/App.tsx"

    def test_the_last_fence_wins(self):
        """An earlier fence is usually the model quoting the schema back."""
        text = (
            "```json\n{\"edits\": [], \"skipped\": []}\n```\n"
            "now the real one\n```json\n" + json.dumps(VALID) + "\n```"
        )
        assert fix_model.extract_json(text)["edits"]

    def test_bare_json_with_no_fence_is_read(self):
        assert fix_model.extract_json(json.dumps(VALID))["edits"]

    def test_an_empty_message_says_so(self):
        with pytest.raises(fix_schema.FixSchemaError, match="no text at all"):
            fix_model.extract_json("   ")

    def test_unparseable_output_carries_a_snippet(self):
        """
        "Expecting value: line 1 column 1" reads the same whether the model
        narrated, returned nothing, or was cut off mid-JSON -- and those need
        three different fixes.
        """
        with pytest.raises(fix_schema.FixSchemaError, match="candidate starts"):
            fix_model.extract_json("I have decided not to answer.")


class TestBudget:
    def test_spending_accumulates_across_calls(self):
        b = fix_model.Budget(1000)
        b.record({"inputTokens": 100, "outputTokens": 20})
        b.record({"inputTokens": 50, "outputTokens": 5})
        assert b.spent == 175 and b.calls == 2

    def test_cache_meters_count_toward_the_budget(self):
        b = fix_model.Budget(1000)
        b.record({"inputTokens": 10, "outputTokens": 1, "cacheReadInputTokens": 90})
        assert b.spent == 101

    def test_the_budget_is_checked_before_a_call_not_during(self):
        """
        A model cannot be stopped mid-generation. A budget that pretends
        otherwise reports a number it did not enforce.
        """
        b = fix_model.Budget(100)
        b.record({"inputTokens": 100, "outputTokens": 50})
        with pytest.raises(fix_model.BudgetExhausted, match="token budget exhausted"):
            b.check()

    def test_an_exhausted_budget_stops_propose_before_it_spends(self):
        b = fix_model.Budget(10)
        b.record({"inputTokens": 100, "outputTokens": 0})
        bedrock = _Bedrock(_response(json.dumps(VALID)))
        with pytest.raises(fix_model.BudgetExhausted):
            fix_model.propose({"id": "F-1"}, {"files": []}, bedrock=bedrock, budget=b)
        assert bedrock.calls == []


class TestPropose:
    def test_a_good_response_is_validated_and_returned(self):
        bedrock = _Bedrock(_response("```json\n" + json.dumps(VALID) + "\n```"))
        out = fix_model.propose(
            {"id": "F-001", "summary": "x"},
            {"files": [{"path": "frontend/src/App.tsx", "text": "a"}], "excluded": []},
            bedrock=bedrock,
            budget=fix_model.Budget(),
        )
        assert out["edits"][0]["finding_id"] == "F-001"

    def test_temperature_is_zero(self):
        """A code-editing call has no use for sampling variance."""
        bedrock = _Bedrock(_response(json.dumps(VALID)))
        fix_model.propose(
            {"id": "F-1"}, {"files": [], "excluded": []},
            bedrock=bedrock, budget=fix_model.Budget(),
        )
        assert bedrock.calls[0]["inferenceConfig"]["temperature"] == 0

    def test_truncation_is_reported_as_truncation(self):
        """
        Not as a parse error. Truncated JSON is invalid, and the fix -- raise the
        output cap -- is different from the fix for a prompt problem.
        """
        bedrock = _Bedrock(_response('```json\n{"edits": [', stop="max_tokens"))
        with pytest.raises(fix_schema.FixSchemaError, match="max_tokens"):
            fix_model.propose(
                {"id": "F-1"}, {"files": [], "excluded": []},
                bedrock=bedrock, budget=fix_model.Budget(),
            )

    def test_usage_is_recorded_even_on_a_response_that_fails_validation(self):
        """The call was billed. A budget that only counts successes understates."""
        budget = fix_model.Budget()
        bedrock = _Bedrock(_response("not json", usage={"inputTokens": 500, "outputTokens": 9}))
        with pytest.raises(fix_schema.FixSchemaError):
            fix_model.propose(
                {"id": "F-1"}, {"files": [], "excluded": []}, bedrock=bedrock, budget=budget
            )
        assert budget.spent == 509

    def test_one_call_per_finding(self):
        bedrock = _Bedrock(_response(json.dumps(VALID)), _response(json.dumps(VALID)))
        budget = fix_model.Budget()
        for fid in ("F-1", "F-2"):
            fix_model.propose(
                {"id": fid}, {"files": [], "excluded": []}, bedrock=bedrock, budget=budget
            )
        assert len(bedrock.calls) == 2


class TestPrompt:
    def test_the_default_model_matches_the_iam_policy_default(self):
        """
        infra/modules/qa_agent/variables.tf scopes fix_model_profile_ids to this
        exact profile. A mismatch is an AccessDenied at invoke time, not a
        fallback to something cheaper.
        """
        assert fix_model.DEFAULT_MODEL == "global.anthropic.claude-sonnet-4-6"

    def test_skipping_is_presented_as_a_good_outcome(self):
        """
        A model that patches on a guess produces a PR that looks complete and is
        wrong, which costs more review time than an honest skip.
        """
        assert "no penalty here for skipping" in fix_prompt.SYSTEM
        assert "is worse than no patch" in fix_prompt.SYSTEM

    def test_the_uniqueness_requirement_is_stated(self):
        assert "EXACTLY ONCE" in fix_prompt.SYSTEM

    def test_scope_creep_is_forbidden_explicitly(self):
        assert "Do not fix a second defect" in fix_prompt.SYSTEM

    def test_withheld_files_are_disclosed_to_the_model(self):
        """
        A model that knows a plausible file was withheld can skip honestly
        instead of forcing a fix into the files it happens to have.
        """
        built = fix_prompt.build(
            {"id": "F-1", "summary": "s"},
            [{"path": "frontend/src/App.tsx", "text": "x"}],
            excluded=[{"path": "frontend/src/Other.tsx", "reason": "byte cap"}],
        )
        assert "Withheld" in built and "frontend/src/Other.tsx" in built

    def test_the_finding_text_reaches_the_prompt(self):
        built = fix_prompt.build(
            {"id": "F-9", "summary": "greeting is duplicated", "page": "/"},
            [{"path": "frontend/src/App.tsx", "text": "x"}],
        )
        assert "F-9" in built and "greeting is duplicated" in built

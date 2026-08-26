"""
Schema validation tests.

These matter more than they look. Prime directive 6 says unparsed or malformed
agent output is a FAILED RUN, not findings -- so this validator is the thing
standing between the PR comment and a table of agent narration presented as
bugs. Every test below is a shape the reference implementation actually let
through.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schema  # noqa: E402


def _finding(**overrides):
    base = {
        "id": "F-001",
        "severity": "HIGH",
        "page": "/voyage",
        "summary": "Fuel chart renders blank",
        "evidence": "Chart container present, zero data points, no console error",
        "steps_to_reproduce": ["Log in", "Open /voyage"],
        "expected": "A fuel curve",
        "actual": "Empty chart area",
        "suspected_source": None,
    }
    base.update(overrides)
    return base


def _payload(**overrides):
    base = {"overall": "FAIL", "pages_tested": 3, "findings": [_finding()]}
    base.update(overrides)
    return base


def test_accepts_a_wellformed_payload():
    assert schema.validate(_payload()) is not None


def test_accepts_a_clean_run_with_no_findings():
    # An empty findings list is a good result, not a suspicious one.
    assert schema.validate({"overall": "PASS", "pages_tested": 8, "findings": []})


def test_rejects_non_object():
    with pytest.raises(schema.SchemaError, match="not a JSON object"):
        schema.validate(["F-001"])


def test_rejects_missing_top_level_key():
    payload = _payload()
    del payload["pages_tested"]
    with pytest.raises(schema.SchemaError, match="pages_tested"):
        schema.validate(payload)


def test_rejects_invalid_severity():
    with pytest.raises(schema.SchemaError, match="severity"):
        schema.validate(_payload(findings=[_finding(severity="BLOCKER")]))


def test_rejects_agent_narration_as_a_finding():
    """
    The exact failure from the reference implementation: narration leaked into
    the findings table as HIGH-severity rows reading "Let me verify this
    further". It has no steps_to_reproduce, so it must not validate.
    """
    narration = {
        "id": "F-002",
        "severity": "HIGH",
        "page": "/",
        "summary": "Let me verify this further",
    }
    with pytest.raises(schema.SchemaError, match="missing required key"):
        schema.validate(_payload(findings=[narration]))


def test_rejects_steps_that_are_not_a_list_of_strings():
    with pytest.raises(schema.SchemaError, match="steps_to_reproduce"):
        schema.validate(_payload(findings=[_finding(steps_to_reproduce="Open /voyage")]))


def test_rejects_pass_that_contradicts_a_blocking_finding():
    # An agent that says PASS while listing a CRITICAL has contradicted itself.
    # Trusting either half would be worse than rejecting the run.
    with pytest.raises(schema.SchemaError, match="PASS but 1 blocking"):
        schema.validate(_payload(overall="PASS", findings=[_finding(severity="CRITICAL")]))


def test_allows_pass_with_only_non_blocking_findings():
    assert schema.validate(_payload(overall="PASS", findings=[_finding(severity="LOW")]))


def test_rejects_duplicate_finding_ids():
    with pytest.raises(schema.SchemaError, match="not unique"):
        schema.validate(_payload(findings=[_finding(), _finding()]))


def test_rejects_negative_pages_tested():
    with pytest.raises(schema.SchemaError, match="non-negative"):
        schema.validate(_payload(pages_tested=-1))


def test_suspected_source_may_be_null_or_string():
    assert schema.validate(_payload(findings=[_finding(suspected_source=None)]))
    assert schema.validate(_payload(findings=[_finding(suspected_source="src/pages/Voyage.tsx")]))


def test_rejects_non_string_suspected_source():
    with pytest.raises(schema.SchemaError, match="suspected_source"):
        schema.validate(_payload(findings=[_finding(suspected_source=42)]))


def test_has_blocking():
    assert schema.has_blocking(_payload(findings=[_finding(severity="CRITICAL")]))
    assert schema.has_blocking(_payload(findings=[_finding(severity="HIGH")]))
    assert not schema.has_blocking(_payload(findings=[_finding(severity="MEDIUM")]))
    assert not schema.has_blocking({"findings": []})


class TestFingerprint:
    """
    Fingerprints drive the Phase 3 convergence check: shrinking finding set means
    continue, identical means stall. If a fingerprint moved between rounds for
    the same defect, every round would look like progress and the loop would
    never detect a stall.
    """

    def test_stable_across_renumbering(self):
        a = schema.finding_fingerprint(_finding(id="F-001"))
        b = schema.finding_fingerprint(_finding(id="F-007"))
        assert a == b

    def test_stable_across_evidence_rewording(self):
        a = schema.finding_fingerprint(_finding(evidence="blank chart"))
        b = schema.finding_fingerprint(_finding(evidence="the chart area was empty"))
        assert a == b

    def test_distinguishes_different_pages(self):
        a = schema.finding_fingerprint(_finding(page="/voyage"))
        b = schema.finding_fingerprint(_finding(page="/ports"))
        assert a != b

    def test_distinguishes_severity_changes(self):
        a = schema.finding_fingerprint(_finding(severity="HIGH"))
        b = schema.finding_fingerprint(_finding(severity="LOW"))
        assert a != b

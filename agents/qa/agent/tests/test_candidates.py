"""
Candidate-findings detector tests.

These pin the MECHANICAL threshold decisions: what counts as a candidate, and
what is noise the model must never be asked to explain. The discriminator run
settled the direction -- sonnet was shown the pristine repeated_svg_empty signal
and still reported nothing -- so the detectors exist to make the "model must
notice" step impossible to skip. False candidates are the other failure: they
would burn the mandatory-assessment contract on decoration, so the warning and
text-kind exclusions are asserted as hard as the detections.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import candidates  # noqa: E402


def test_repeated_svg_empty_is_detected_with_count_and_capped_samples():
    """The S-2 shape exactly: 13 svg-adjacent blanks rolled into one group."""
    out = candidates.detect({
        "repeated_slots": [
            {"kind": "svg-adjacent", "count": 13, "sample": ["a", "b", "c", "d"]}
        ],
    })
    assert len(out) == 1
    cand = out[0]
    assert cand["type"] == "repeated_svg_empty"
    assert cand["count"] == 13
    # Samples are capped so one candidate cannot dump a wall of context.
    assert cand["samples"] == ["a", "b", "c"]


def test_a_single_svg_blank_is_not_a_candidate():
    """A lone hint stays a hint; only the repetition is mechanical evidence."""
    out = candidates.detect({"repeated_slots": [{"kind": "svg-adjacent", "count": 1, "sample": []}]})
    assert out == []


def test_text_kind_repetition_is_not_a_candidate():
    """Badge dots aggregate as text-kind groups on every page; never ask about them."""
    out = candidates.detect({
        "repeated_slots": [{"kind": "text", "count": 5, "sample": ["dot"]}]
    })
    assert out == []


def test_warning_console_entries_are_noise():
    out = candidates.detect({"console_errors": ["warning: manifest icon failed to load"]})
    assert out == []


def test_genuine_console_errors_are_detected():
    out = candidates.detect({
        "console_errors": ["uncaught: TypeError: t.map is not a function", "error: failed to parse"]
    })
    assert len(out) == 1
    cand = out[0]
    assert cand["type"] == "console_error"
    assert cand["count"] == 2


def test_failed_requests_are_detected():
    out = candidates.detect({"failed_requests": ["net::ERR_FAILED: XHR"]})
    assert len(out) == 1
    assert out[0]["type"] == "failed_request"
    assert out[0]["count"] == 1


def test_multiple_candidate_classes_are_all_reported():
    out = candidates.detect({
        "repeated_slots": [{"kind": "svg-adjacent", "count": 2, "sample": []}],
        "console_errors": ["error: boom"],
        "failed_requests": ["net::ERR_FAILED"],
    })
    assert {c["type"] for c in out} == {"repeated_svg_empty", "console_error", "failed_request"}


def test_empty_state_yields_no_candidates():
    assert candidates.detect({}) == []
    assert candidates.detect({"repeated_slots": None}) == []
    assert candidates.detect({"console_errors": None, "failed_requests": None}) == []


def test_warning_and_errors_mixed_only_errors_are_candidates():
    out = candidates.detect({
        "console_errors": ["warning: favicon 404", "uncaught: TypeError: x is undefined"]
    })
    assert out[0]["count"] == 1
    assert "TypeError" in out[0]["samples"][0]

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


class TestStatusCaseLeak:
    """
    The S-3 mechanical half. The seed's leak is a status value whose STORED
    spelling is all-caps ("OPEN") on a page that renders the same class of value
    lowercase elsewhere ("closed"). The harvest walks text nodes -- the SOURCE
    case, which CSS text-transform never touches -- so CSS-uppercased labels
    ("ACTIVE", "GREEN") can never reach `case_words`. Fires only on the
    inconsistency; if every status is uppercase, uppercase is the convention.
    """

    def test_a_status_value_leaking_raw_is_detected(self):
        """The corpus /sire shape exactly: OPEN all-caps, closed lowercase."""
        out = candidates.detect({
            "case_words": [
                {"word": "OPEN", "count": 4, "sample": ["OPEN", "closed"]},
                {"word": "IMO", "count": 2, "sample": ["IMO 9432521"]},
            ],
            "text": "Readiness\nOPEN\nclosed\nOPEN\nOPEN\nOPEN",
        })
        assert len(out) == 1
        cand = out[0]
        assert cand["type"] == "status_case_leak"
        assert cand["count"] == 4
        assert "OPEN" in cand["evidence"]
        assert "closed" in cand["evidence"]

    def test_a_capitalize_css_twin_still_fires(self):
        """
        The corpus /sire status span carries a `capitalize` class, so innerText
        shows 'Closed', not 'closed'. A twin is any NON-uppercase rendering of a
        status word -- requiring the fully-lowercase form verbatim would make the
        candidate impossible to fire on the exact page it exists for (this shape
        caused the S-3 miss in P1.2 run 1).
        """
        out = candidates.detect({
            "case_words": [
                {"word": "OPEN", "count": 4, "sample": ["OPEN", "Closed"]},
            ],
            "text": "SIRE findings\nOPEN\nClosed\nOPEN\nOPEN\nOPEN\nCh.1\nCh.2",
        })
        assert len(out) == 1
        cand = out[0]
        assert cand["type"] == "status_case_leak"
        assert cand["count"] == 4
        assert "closed" in cand["evidence"]

    def test_acronyms_are_not_statuses(self):
        """IMO/CII/MT survive the walk but are filtered by the vocabulary."""
        out = candidates.detect({
            "case_words": [{"word": "IMO", "count": 2, "sample": []}],
            "text": "MT Aurora, IMO 9432521. Status: closed.",
        })
        assert out == []

    def test_all_caps_is_the_convention_when_no_normal_case_status_renders(self):
        """A page that stores every status uppercase is consistent, not leaking."""
        out = candidates.detect({
            "case_words": [{"word": "OPEN", "count": 4, "sample": []}],
            "text": "OPEN CLOSED OPEN",
        })
        assert out == []

    def test_css_uppercased_labels_never_reach_case_words(self):
        """
        The harvest only reports all-caps from the SOURCE case, so a page whose
        uppercase is text-transform (badges, headers) yields no case_words at
        all -- nothing to compare, nothing to fire on.
        """
        out = candidates.detect({
            "text": "ACTIVE  GREEN  CHAPTERS  OPEN FINDINGS",
        })
        assert out == []

    def test_a_page_with_no_all_caps_words_yields_nothing(self):
        out = candidates.detect({
            "case_words": [],
            "text": "open, closed, active",
        })
        assert out == []

    def test_case_leak_coexists_with_other_candidate_classes(self):
        out = candidates.detect({
            "case_words": [{"word": "OPEN", "count": 1, "sample": []}],
            "text": "closed",
            "console_errors": ["uncaught: boom"],
        })
        assert {c["type"] for c in out} == {"status_case_leak", "console_error"}

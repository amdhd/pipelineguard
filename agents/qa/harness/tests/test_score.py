"""
Scorer tests.

The scorer's only job is to produce two numbers that can be trusted. So what
matters here is mostly what it REFUSES to claim: a rate over a half-labelled run,
a detection it inferred from a loose string match, a zero that means "nobody has
looked yet".
"""

import sys
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(_HARNESS.parent / "agent"))

import schema  # noqa: E402
import score  # noqa: E402


def _f(page="/voyage", severity="HIGH", summary="Fuel chart renders blank", **over):
    f = {
        "id": "F-001",
        "severity": severity,
        "page": page,
        "summary": summary,
        "evidence": "Chart container present, zero data points",
        "steps_to_reproduce": ["Log in"],
        "expected": "A fuel curve",
        "actual": "Empty chart",
        "suspected_source": None,
    }
    f.update(over)
    return f


def _run(*findings):
    return {"overall": "FAIL", "pages_tested": 8, "findings": list(findings)}


class TestFalsePositiveRate:
    def test_unlabelled_findings_do_not_become_correct_ones(self):
        """
        The failure this guards: a run nobody has reviewed scoring 0% false
        positives, which reads as a triumph and means nothing.
        """
        result = score.score(_run(_f()), {})
        assert result["false_positive_rate"] is None
        assert result["unlabelled"] == 1
        assert "not measured" in score.render(result)

    def test_rate_is_computed_over_labelled_findings_only(self):
        a, b, c = _f(id="F-001"), _f(id="F-002", page="/ports"), _f(id="F-003", page="/sire")
        corpus = {
            "labels": {
                schema.finding_fingerprint(a): score.TRUE_POSITIVE,
                schema.finding_fingerprint(b): score.FALSE_POSITIVE,
            }
        }
        result = score.score(_run(a, b, c), corpus)
        assert result["labelled"] == 2
        assert result["false_positive_rate"] == pytest.approx(0.5)
        assert result["unlabelled"] == 1

    def test_by_design_counts_against_the_rate(self):
        """
        It cost the reviewer the same. The label stays distinct because the fix
        differs -- by-design means the rubric has drifted from the target's own
        "what's real vs mocked" list, not that the severity ladder is wrong.
        """
        a = _f()
        corpus = {"labels": {schema.finding_fingerprint(a): score.BY_DESIGN}}
        assert score.score(_run(a), corpus)["false_positive_rate"] == pytest.approx(1.0)

    def test_the_report_names_what_still_needs_a_human(self):
        result = score.score(_run(_f()), {})
        assert "awaiting review" in score.render(result)
        assert schema.finding_fingerprint(_f()) in score.render(result)


class TestFalseNegativeRate:
    _SEEDS = [
        {"id": "S-1", "page": "/voyage", "keywords": ["blank", "no data"]},
        {"id": "S-2", "page": "/maintenance", "keywords": ["NaN"]},
    ]

    def test_a_seeded_bug_the_agent_found(self):
        result = score.score(_run(_f(page="/voyage", summary="chart is blank")), {"seeded": self._SEEDS})
        assert "S-1" in result["seeded_detected"]
        assert "S-2" in result["seeded_missed"]
        assert result["false_negative_rate"] == pytest.approx(0.5)

    def test_a_clean_run_against_seeded_bugs_is_a_100_percent_miss(self):
        """
        The measurement no amount of watching real PRs provides: an agent that
        reports nothing looks identical whether the app is clean or it is blind.
        """
        result = score.score(_run(), {"seeded": self._SEEDS})
        assert result["false_negative_rate"] == pytest.approx(1.0)
        assert result["seeded_missed"] == ["S-1", "S-2"]

    def test_the_right_bug_on_the_wrong_page_is_not_a_detection(self):
        result = score.score(_run(_f(page="/ports", summary="chart is blank")), {"seeded": self._SEEDS})
        assert result["seeded_detected"] == []

    def test_a_human_label_can_override_the_keyword_match(self):
        """
        Keyword matching is deliberately narrow, so a real detection phrased
        differently would be scored a miss. A human says otherwise and wins.
        """
        f = _f(page="/voyage", summary="fuel series renders as an empty axis")
        corpus = {"seeded": self._SEEDS, "labels": {schema.finding_fingerprint(f): "S-1"}}
        assert "S-1" in score.score(_run(f), corpus)["seeded_detected"]

    def test_no_seeds_means_no_claim(self):
        assert score.score(_run(_f()), {})["false_negative_rate"] is None


class TestRungComparison:
    def test_identical_runs_agree_completely(self):
        run = _run(_f())
        assert score.compare(run, run)["agreement"] == pytest.approx(1.0)

    def test_findings_unique_to_one_rung_are_surfaced(self):
        cheap = _run(_f(id="F-001"), _f(id="F-002", page="/ports"))
        quality = _run(_f(id="F-001"))
        result = score.compare(cheap, quality)
        assert len(result["a_only"]) == 1
        assert result["both"] and not result["b_only"]
        assert result["agreement"] == pytest.approx(0.5)

    def test_two_empty_runs_make_no_claim(self):
        assert score.compare(_run(), _run())["agreement"] is None

    def test_comparison_ignores_finding_ids_which_are_renumbered_every_run(self):
        """
        Identity across runs is the fingerprint, not F-001 -- ids are positional
        and would make two identical runs look completely different.
        """
        a = _run(_f(id="F-001"))
        b = _run(_f(id="F-047"))
        assert score.compare(a, b)["agreement"] == pytest.approx(1.0)

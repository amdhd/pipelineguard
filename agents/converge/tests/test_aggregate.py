"""
Repeated QA runs, majority-aggregated into one round (P1.3).

One QA run is not a measurement -- EVIDENCE.md measured S-1 caught in five runs
and missed in the sixth on identical code. These tests pin down the vote that
turns K runs into the one findings JSON the loop already consumes: what counts,
what does not, and what the aggregate says when the round itself is suspect.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aggregate  # noqa: E402
import convergence as conv  # noqa: E402
import session as sess  # noqa: E402
from rounds import A, B, C, qa  # noqa: E402


def write(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload))
    return str(path)


def with_commit(payload: dict, commit: str = "cafe000") -> dict:
    payload["observed_at_commit"] = commit
    return payload


class TestAggregateFindings:
    def test_single_run_passes_through_unchanged(self):
        one = with_commit(qa([A, B]))
        assert conv.aggregate_findings([one]) is one

    def test_a_finding_in_two_of_three_runs_is_kept(self):
        agg = conv.aggregate_findings([qa([A]), qa([A]), qa([])])
        assert [f["summary"] for f in agg["findings"]] == [A["summary"]]

    def test_a_finding_in_one_of_three_runs_is_dropped(self):
        agg = conv.aggregate_findings([qa([A]), qa([]), qa([])])
        assert agg["findings"] == []

    def test_severity_is_the_most_severe_grade_across_runs(self):
        """
        The same defect graded HIGH by one run and MEDIUM by another is ONE
        finding, graded at its most severe -- `finding_fingerprint` embeds
        severity, so grouping by it would have split this into two rows.
        """
        med = {**A, "severity": "MEDIUM"}
        agg = conv.aggregate_findings([qa([med]), qa([A]), qa([med])])
        assert len(agg["findings"]) == 1
        assert agg["findings"][0]["severity"] == "HIGH"

    def test_a_minority_high_grade_stays_blocking(self):
        """
        The conservative direction, documented: the defect is in all three runs,
        and one of them graded it HIGH. Merging to MEDIUM would have dropped it
        below the blocking line on a run where a real blocker was reported.
        """
        med = {**A, "severity": "MEDIUM"}
        agg = conv.aggregate_findings([qa([med]), qa([med]), qa([A])])
        assert agg["findings"][0]["severity"] == "HIGH"
        assert agg["overall"] == "FAIL"

    def test_observed_at_commit_is_preserved(self):
        runs = [with_commit(qa([A]), "deadbeef") for _ in range(3)]
        assert conv.aggregate_findings(runs)["observed_at_commit"] == "deadbeef"

    def test_majority_failure_makes_the_round_inconclusive(self):
        runs = [qa([A], error="runtime_unavailable")] * 2 + [qa([A])]
        agg = conv.aggregate_findings(runs)
        assert agg["error"] == "runtime_unavailable"
        assert agg["findings"] == []
        assert "2 of 3" in agg["detail"]

    def test_a_single_error_run_does_not_fold_its_findings_in(self):
        # Two clean runs agree on A; the failed run's unique finding is not a
        # majority and is excluded with it.
        runs = [qa([A]), qa([A]), qa([C], error="runtime_unavailable")]
        agg = conv.aggregate_findings(runs)
        assert [f["summary"] for f in agg["findings"]] == [A["summary"]]
        assert agg["repeats"]["completed"] == 2

    def test_all_runs_error_propagates_the_first_error(self):
        runs = [qa([], error="timeout"), qa([], error="schema_violation"), qa([], error="timeout")]
        assert conv.aggregate_findings(runs)["error"] == "timeout"

    def test_empty_findings_aggregate_is_a_pass(self):
        agg = conv.aggregate_findings([qa([]), qa([]), qa([])])
        assert agg["overall"] == "PASS"
        assert agg["findings"] == []

    def test_majority_blocking_makes_overall_fail(self):
        assert conv.aggregate_findings([qa([A]), qa([A]), qa([A])])["overall"] == "FAIL"

    def test_ids_are_regenerated_sequentially(self):
        agg = conv.aggregate_findings([qa([A, B]), qa([A, B]), qa([A, B])])
        assert [f["id"] for f in agg["findings"]] == ["F-001", "F-002"]

    def test_cost_and_session_seconds_sum_across_runs(self):
        runs = [qa([A], tokens=1000, seconds=30) for _ in range(3)]
        agg = conv.aggregate_findings(runs)
        assert agg["cost"]["model_tokens"]["input"] == 3000
        assert agg["session_seconds"] == 90

    def test_even_k_needs_a_strict_majority(self):
        # K=2: a finding in 1 of 2 runs is no agreement (ceil(2/2) would keep it).
        agg = conv.aggregate_findings([qa([A]), qa([])])
        assert agg["findings"] == []

    def test_a_threshold_can_be_passed_explicitly(self):
        # threshold=1 is the union: any run reporting it counts.
        agg = conv.aggregate_findings([qa([A]), qa([]), qa([])], threshold=1)
        assert [f["summary"] for f in agg["findings"]] == [A["summary"]]

    def test_no_runs_is_a_programming_error(self):
        with pytest.raises(ValueError):
            conv.aggregate_findings([])


class TestAggregateCli:
    def test_cli_merges_three_files_and_writes_aggregate(self, tmp_path):
        paths = [write(tmp_path / f"r{i}.json", qa([A])) for i in (1, 2, 3)]
        out = tmp_path / "agg.json"
        code = aggregate.run(
            aggregate.build_parser().parse_args(
                ["--findings", *paths, "--json-out", str(out)]
            )
        )
        assert code == 0
        agg = json.loads(out.read_text())
        assert [f["summary"] for f in agg["findings"]] == [A["summary"]]

    def test_cli_single_file_is_passthrough(self, tmp_path):
        path = write(tmp_path / "r1.json", qa([A, B]))
        out = tmp_path / "agg.json"
        code = aggregate.run(
            aggregate.build_parser().parse_args(
                ["--findings", path, "--json-out", str(out)]
            )
        )
        assert code == 0
        assert json.loads(out.read_text()) == json.loads(Path(path).read_text())

    def test_cli_corrupt_input_exits_2(self, tmp_path):
        (tmp_path / "r1.json").write_text("{not json")
        out = tmp_path / "agg.json"
        code = aggregate.run(
            aggregate.build_parser().parse_args(
                ["--findings", str(tmp_path / "r1.json"), "--json-out", str(out)]
            )
        )
        assert code == 2
        assert not out.exists()

    def test_cli_non_object_input_exits_2(self, tmp_path):
        write(tmp_path / "r1.json", [])
        code = aggregate.run(
            aggregate.build_parser().parse_args(
                ["--findings", str(tmp_path / "r1.json"), "--json-out", str(tmp_path / "agg.json")]
            )
        )
        assert code == 2

    def test_an_aggregated_round_feeds_the_session_like_a_single_run(self, tmp_path):
        """
        The drop-in claim: the aggregate is handed to session.run exactly where
        a single run's findings file used to be, and the round records normally.
        """
        paths = [write(tmp_path / f"r{i}.json", payload) for i, payload in enumerate(
            [qa([A, B]), qa([A, B]), qa([A])]
        )]
        agg_path = tmp_path / "findings-0.json"
        code = aggregate.run(
            aggregate.build_parser().parse_args(
                ["--findings", *paths, "--json-out", str(agg_path)]
            )
        )
        assert code == 0

        exit_code = sess.run(
            sess.build_parser().parse_args(
                [
                    "--state", str(tmp_path / "state.json"),
                    "--findings", str(agg_path),
                    "--comment-out", str(tmp_path / "comment.md"),
                ]
            )
        )
        assert exit_code == 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert len(state["rounds"]) == 1
        assert state["rounds"][0]["blocking"]
        assert state["decisions"][0]["state"] == conv.CONTINUE

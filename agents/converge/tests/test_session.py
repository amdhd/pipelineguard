"""
The CLI, driven the way the workflow drives it.

Every test here is a sequence of invocations against real files on disk, because
that is the only thing being claimed: that a loop written as a series of shell
steps, each one a fresh process, still behaves like a loop. The state file is
the whole mechanism, so it is what gets exercised.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import convergence as conv  # noqa: E402
import session  # noqa: E402
from rounds import A, B, fix, qa  # noqa: E402


def write(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload))
    return str(path)


def call(tmp_path, findings, fix_result=None, *, extra=(), state="state.json"):
    """One round, as the workflow runs it. Returns (exit code, state, comment)."""
    n = len(list(tmp_path.glob("findings-*.json")))
    argv = [
        "--state", str(tmp_path / state),
        "--findings", write(tmp_path / f"findings-{n}.json", findings),
        "--comment-out", str(tmp_path / "comment.md"),
        *extra,
    ]
    if fix_result is not None:
        argv += ["--fix-result", write(tmp_path / f"fix-{n}.json", fix_result)]
    code = session.run(session.build_parser().parse_args(argv))
    return (
        code,
        json.loads((tmp_path / state).read_text()),
        (tmp_path / "comment.md").read_text(),
    )


class TestTheStateFile:
    def test_a_missing_state_file_is_round_one_not_an_error(self, tmp_path):
        code, state, _ = call(tmp_path, qa([A, B]))
        assert code == 0
        assert len(state["rounds"]) == 1
        assert state["decisions"][0]["state"] == conv.CONTINUE

    def test_rounds_accumulate_across_invocations(self, tmp_path):
        call(tmp_path, qa([A, B]), fix(applied=["F-002"]))
        _, state, _ = call(tmp_path, qa([A]))
        assert [r["round"] for r in state["rounds"]] == [1, 2]
        assert state["decisions"][-1]["state"] == conv.CONTINUE

    def test_the_round_number_comes_from_the_state_not_from_a_flag(self, tmp_path):
        """
        There is deliberately no --round. A loop written in YAML is exactly the
        place an off-by-one arrives, and a round compared against itself reports
        a stall on a run that was converging.
        """
        assert "--round\b" not in session.build_parser().format_help()
        call(tmp_path, qa([A, B]))
        _, state, _ = call(tmp_path, qa([A]))
        assert state["rounds"][-1]["round"] == 2

    def test_an_empty_state_file_is_treated_as_no_state(self, tmp_path):
        (tmp_path / "state.json").write_text("")
        code, state, _ = call(tmp_path, qa([A]))
        assert code == 0
        assert len(state["rounds"]) == 1

    def test_budgets_are_re_read_from_the_flags_every_round(self, tmp_path):
        """
        The caps belong to the workflow that spends the money. A state file
        carrying its own would let a stale artefact quietly raise them.
        """
        call(tmp_path, qa([A, B]), extra=["--max-rounds", "9"])
        _, state, _ = call(tmp_path, qa([A]), extra=["--max-rounds", "2"])
        assert state["budgets"]["max_rounds"] == 2
        assert state["decisions"][-1]["state"] == conv.MAX_ROUNDS


class TestOutputs:
    def test_the_workflow_reads_decision_stop_and_round(self, tmp_path):
        out = tmp_path / "gh-output"
        call(tmp_path, qa([A, B]), extra=["--github-output", str(out)])
        written = dict(line.split("=", 1) for line in out.read_text().splitlines())
        assert written == {"decision": "CONTINUE", "stop": "false", "round": "1"}

    def test_a_stop_is_signalled_as_such(self, tmp_path):
        out = tmp_path / "gh-output"
        call(tmp_path, qa([]), extra=["--github-output", str(out)])
        assert "stop=true" in out.read_text()
        assert "decision=PASS" in out.read_text()

    def test_the_output_file_is_appended_not_replaced(self, tmp_path):
        """
        $GITHUB_OUTPUT is shared by every step in the job. Truncating it would
        discard outputs other steps had already written.
        """
        out = tmp_path / "gh-output"
        out.write_text("existing=1\n")
        call(tmp_path, qa([A]), extra=["--github-output", str(out)])
        assert out.read_text().startswith("existing=1\n")

    def test_the_comment_is_written_for_every_round(self, tmp_path):
        _, _, comment = call(tmp_path, qa([A, B]))
        assert comment.startswith("<!-- pipelineguard-qa-converge -->")


class TestExitCodes:
    def test_continue_and_pass_are_both_zero(self, tmp_path):
        assert call(tmp_path, qa([A, B]))[0] == 0
        # A second, independent loop -- its own state file, so it is round 1.
        assert call(tmp_path, qa([]), state="other.json")[0] == 0

    def test_a_stall_exits_non_zero(self, tmp_path):
        call(tmp_path, qa([A, B]))
        code, state, _ = call(tmp_path, qa([A, B]))
        assert state["decisions"][-1]["state"] == conv.STALL
        assert code == 1

    def test_an_inconclusive_round_exits_non_zero(self, tmp_path):
        code, state, _ = call(tmp_path, qa([], error="runtime_unavailable"))
        assert state["decisions"][-1]["state"] == conv.INCONCLUSIVE
        assert code == 1


class TestTheWholeLoop:
    def test_a_run_that_converges(self, tmp_path):
        """
        The happy path end to end: two blocking findings, one patched each round,
        and a third round that observes none. The reconciliation must credit both
        patches -- this is the only place "fixed in round 1" is recoverable.
        """
        call(tmp_path, qa([A, B]), fix(applied=["F-002"]))
        call(tmp_path, qa([A]), fix(applied=["F-001"]))
        code, state, comment = call(tmp_path, qa([]))

        assert code == 0
        assert state["decisions"][-1]["state"] == conv.PASS
        assert comment.count(conv.FIXED) == 2
        assert "### Reconciliation" in comment

    def test_a_run_that_regresses_names_the_new_finding(self, tmp_path):
        call(tmp_path, qa([A]), fix(applied=["F-001"]))
        code, state, comment = call(tmp_path, qa([B]))
        assert code == 1
        assert state["decisions"][-1]["state"] == conv.REGRESSED
        assert conv.INTRODUCED in comment

    def test_labels_reach_the_ledger(self, tmp_path):
        labels = write(
            tmp_path / "corpus.json",
            {"labels": {conv.schema.finding_fingerprint(A): "by-design"}},
        )
        call(tmp_path, qa([A]))
        _, _, comment = call(tmp_path, qa([A]), extra=["--labels", labels])
        assert "by-design" in comment

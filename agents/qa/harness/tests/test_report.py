"""
Renderer tests for the `🔁 Prior findings re-verified` block (D-2).

The block is the PR-facing half of the reconciliation ledger. report.py renders
rows that `converge.verify_report` produced, so this file checks the rendering
contract: statuses carry their icon and verbatim upstream vocabulary, the block
lands in the comment only when rows are supplied, it always sits above the cost
table, and an UNVERIFIED row explains itself rather than leaving a bare table a
reader could mistake for progress.
"""

import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(_HARNESS.parent / "agent"))

import report  # noqa: E402


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


def _row(status, *, page="/voyage", severity="HIGH", summary="Fuel chart renders blank", fingerprint="fp"):
    """A reconciliation row exactly as `converge.verify_report` emits one."""
    return {
        "fingerprint": fingerprint,
        "severity": severity,
        "page": page,
        "summary": summary,
        "status": status,
    }


class TestReverifyBlock:
    def test_every_status_renders_with_its_icon_and_vocabulary(self):
        out = report.render_reverify(
            [
                _row("still failing"),
                _row("fixed", severity="MEDIUM"),
                _row("not reproduced", severity="LOW"),
                _row("unverified"),
            ]
        )
        assert "Prior findings re-verified" in out
        assert "🔴" in out and "STILL FAILING" in out
        assert "✅" in out and "FIXED" in out
        assert "NOT REPRODUCED" in out
        assert "⏸" in out and "UNVERIFIED" in out

    def test_block_sits_above_the_cost_table(self):
        """The re-verify ledger is a product of the run, so it belongs with the
        findings, not buried under the bill."""
        out = report.render(_findings(), reverify_rows=[_row("still failing")])
        assert "Prior findings re-verified" in out
        assert out.index("Prior findings re-verified") < out.index("### Cost")

    def test_no_block_when_no_rows_are_supplied(self):
        assert "Prior findings re-verified" not in report.render(_findings(findings=[]))

    def test_empty_rows_render_nothing(self):
        assert report.render_reverify([]) == ""

    def test_an_errored_rereport_still_shows_the_unverified_ledger(self):
        """
        A re-run that failed re-measured nothing, but the prior findings still
        need their verdict -- UNVERIFIED -- and that verdict only reaches a
        reader if the block is rendered even on the failure headline.
        """
        out = report.render(
            {"error": "schema_violation", "detail": "not JSON", "findings": []},
            reverify_rows=[_row("unverified")],
        )
        assert "run failed" in out
        assert "Prior findings re-verified" in out
        assert "UNVERIFIED" in out

    def test_unverified_rows_carry_the_caveat_not_a_bare_table(self):
        out = report.render_reverify([_row("unverified")])
        assert "never re-tested" in out

    def test_finding_text_with_a_pipe_does_not_break_the_table(self):
        """Markdown tables treat `|` as a column separator."""
        out = report.render_reverify([_row("still failing", summary="billing | amounts column")])
        assert "billing \\| amounts column" in out


def _board_row(status, *, source="origin-run", label=None, **over):
    """A board row exactly as the harness's _board_rows emits one: the re-verify
    shape plus the D-4 attribution (source) and the human label slot."""
    r = _row(status, **over)
    r["source"] = source
    r["label"] = label
    return r


class TestBoard:
    """The D-4 reconciliation board renderer."""

    def test_the_header_is_final_only_when_a_row_is_fixed(self):
        """
        A clean run that closed a fix leg reads as a FINAL board; a clean re-run
        with no fix chain must not overclaim, so it reads as a plain
        Reconciliation board.
        """
        final = report.render_board(
            [_board_row("fixed"), _board_row("not reproduced", severity="LOW")]
        )
        assert "Final reconciliation board" in final
        assert "1 fixed" in final

        plain = report.render_board(
            [_board_row("not reproduced"), _board_row("still failing")]
        )
        assert "Reconciliation board" in plain
        assert "Final reconciliation board" not in plain
        assert "no finding was fixed" in plain

    def test_every_status_keeps_the_reverify_vocabulary(self):
        out = report.render_board([_board_row("fixed"), _board_row("not reproduced"), _board_row("still failing", severity="MEDIUM")])
        assert "STILL FAILING" in out and "FIXED" in out and "NOT REPRODUCED" in out

    def test_unlabelled_rows_are_marked_and_explained(self):
        out = report.render_board([_board_row("fixed")])
        assert "_needs a human_" in out
        assert "never counted as resolved" in out

    def test_a_human_label_is_rendered_verbatim(self):
        out = report.render_board([_board_row("fixed", label="true-positive")])
        assert "true-positive" in out
        assert "_needs a human_" not in out
        assert "never counted as resolved" not in out

    def test_a_fix_verdict_attribution_names_its_provenance(self):
        out = report.render_board(
            [_board_row("fixed", source="fix-verdict"), _board_row("not reproduced", severity="LOW")]
        )
        assert "fix-verdict.json" in out
        assert "reconciled against" in out

    def test_the_board_replaces_the_plain_reverify_table(self):
        """
        The board is a superset of the re-verify table, so a run that has a
        board renders it instead of both blocks.
        """
        out = report.render(
            _findings(findings=[]),
            reverify_rows=[_row("still failing")],
            board_rows=[_board_row("fixed")],
        )
        assert "Final reconciliation board" in out
        assert "Prior findings re-verified" not in out

    def test_no_board_when_no_rows_are_supplied(self):
        assert "Reconciliation board" not in report.render(_findings(findings=[]))
        assert report.render_board([]) == ""


class TestUnmatched:
    """The `New findings not in the origin report` note (D-4)."""

    def test_a_new_blocking_finding_carries_the_fail_caveat(self):
        out = report.render_unmatched([_finding(id="F-9", severity="HIGH", summary="new regressed tab")])
        assert "New findings not in the origin report" in out
        assert "HIGH/CRITICAL and fail this check" in out
        assert "new regressed tab" in out

    def test_a_non_blocking_new_finding_does_not_claim_to_fail(self):
        out = report.render_unmatched([_finding(id="F-9", severity="LOW", summary="cosmetic")])
        assert "fail this check" not in out

    def test_empty_renders_nothing(self):
        assert report.render_unmatched([]) == ""

    def test_render_appends_the_note_after_the_board(self):
        out = report.render(
            _findings(findings=[]),
            board_rows=[_board_row("fixed")],
            unmatched=[_finding(id="F-9", summary="regressed tab")],
        )
        assert out.index("New findings not in the origin report") > out.index("Final reconciliation board")

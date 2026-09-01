"""
The report.

Two things are being protected here. One is that a stop state always tells the
reader what to DO -- a loop that spent three rounds and ends on a bare state name
has wasted the most expensive part of its own output. The other is that the cost
section stays honest under partial knowledge: unpriced meters and unaccounted
fix tokens both have to survive into the rendered table, because a total that
quietly drops them is the failure PRICING.md exists to prevent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import convergence as conv  # noqa: E402
import ledger  # noqa: E402
from rounds import A, B, PRICES, fix, qa, rnd  # noqa: E402


class TestStructure:
    def test_the_marker_is_present_and_distinct_from_the_qa_comment(self):
        """
        A separate marker because both comments land on the same PR. Sharing one
        would make whichever tool updates in place overwrite the other's report.
        """
        out = ledger.render([rnd(1, [A])], conv.decide([rnd(1, [A])]))
        assert ledger.MARKER in out
        assert ledger.MARKER != "<!-- pipelineguard-qa-agent -->"

    def test_the_headline_carries_the_state_and_the_round_count(self):
        rounds = [rnd(1, [A, B]), rnd(2, [A, B])]
        out = ledger.render(rounds, conv.decide(rounds))
        assert "Convergence loop — STALL (2 rounds)" in out

    def test_one_round_is_not_pluralised(self):
        assert "(1 round)" in ledger.render([rnd(1, [])], conv.decide([rnd(1, [])]))

    def test_the_reason_is_rendered_not_just_the_state(self):
        rounds = [rnd(1, [A, B]), rnd(2, [A, B])]
        decision = conv.decide(rounds)
        # Capitalised for the comment; the sentence is otherwise unchanged.
        assert decision.reason[1:] in ledger.render(rounds, decision)

    def test_every_state_has_a_next_action(self):
        """
        A guard against adding a state and leaving the reader without guidance.
        The states are enumerated from the module rather than listed by hand, so
        a new one fails this test instead of shipping silently.
        """
        states = [
            v for k, v in vars(conv).items()
            if k.isupper() and isinstance(v, str) and v == k
        ]
        assert len(states) >= 9
        for state in states:
            assert ledger.NEXT.get(state), state
            assert ledger.ICON.get(state), state


class TestTheRoundsTable:
    def test_the_first_round_shows_no_comparison(self):
        """
        "0 resolved" on round 1 reads as a stall. There was nothing to resolve.
        """
        out = ledger.render([rnd(1, [A, B])], conv.decide([rnd(1, [A, B])]))
        row = [ln for ln in out.splitlines() if ln.startswith("| 1 ")][0]
        assert row.count("—") >= 2

    def test_a_later_round_shows_what_moved(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-002"])), rnd(2, [A])]
        out = ledger.render(rounds, conv.decide(rounds))
        row = [ln for ln in out.splitlines() if ln.startswith("| 2 ")][0]
        assert "| 1 | 1 | 0 |" in row  # 1 blocking, 1 resolved, 0 new, 0 patched

    def test_a_failed_round_is_annotated_in_its_own_row(self):
        """
        Otherwise round 3's empty finding set looks like the best round of the
        run rather than the one that never reached the application.
        """
        rounds = [rnd(1, [A]), rnd(2, [], error="runtime_unavailable")]
        out = ledger.render(rounds, conv.decide(rounds))
        assert "failed: runtime_unavailable" in out

    def test_a_partial_round_names_the_reason_it_stopped(self):
        rounds = [rnd(1, [A]), rnd(2, [A], incomplete=True, stop_reason="wall clock")]
        out = ledger.render(rounds, conv.decide(rounds))
        assert "partial: wall clock" in out


class TestTheLedger:
    def test_it_is_absent_while_the_loop_is_still_running(self):
        """
        PLAN.md asks for the reconciliation "on the final round". Mid-loop it
        would be a history of nothing, and the outcomes would be wrong: a
        finding about to be fixed in the next round renders as outstanding.
        """
        rounds = [rnd(1, [A, B])]
        out = ledger.render(rounds, conv.decide(rounds))
        assert conv.decide(rounds).state == conv.CONTINUE
        assert "### Reconciliation" not in out

    def test_it_appears_on_every_stop(self):
        rounds = [rnd(1, [A, B]), rnd(2, [A, B])]
        assert "### Reconciliation" in ledger.render(rounds, conv.decide(rounds))

    def test_it_names_the_round_a_finding_was_patched_in(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-002"])), rnd(2, [A]), rnd(3, [A])]
        out = ledger.render(rounds, conv.decide(rounds))
        assert conv.FIXED in out
        assert conv.PATCH_INEFFECTIVE in out or conv.OUTSTANDING in out

    def test_an_unlabelled_finding_asks_for_a_human(self):
        rounds = [rnd(1, [A]), rnd(2, [A])]
        assert "_needs a human_" in ledger.render(rounds, conv.decide(rounds))

    def test_a_supplied_label_replaces_the_prompt(self):
        rounds = [rnd(1, [A]), rnd(2, [A])]
        labels = {conv.schema.finding_fingerprint(A): "by-design"}
        out = ledger.render(rounds, conv.decide(rounds), labels)
        assert "by-design" in out


class TestCost:
    def test_an_unpriced_run_says_so_and_still_reports_units(self):
        cheap = conv.round_record(
            1, qa([A], tokens=1234), None, seconds=60,
            prices={"models": {}, "compute": PRICES["compute"]},
        )
        out = ledger.render([cheap, cheap], conv.decide([cheap, cheap]))
        assert "unpriced" in out
        assert "2,468" in out

    def test_unaccounted_fix_tokens_are_called_a_floor(self):
        """
        The number the token backstop enforces against. Rendering it as a total
        when a round's fix run reported nothing would understate the run in the
        one place the understatement costs money.
        """
        rounds = [rnd(1, [A, B], fix(applied=["F-001"], budget=False)), rnd(2, [A])]
        out = ledger.render(rounds, conv.decide(rounds))
        assert "a floor" in out
        assert "floor rather than a total" in out

    def test_the_cumulative_cost_covers_every_round(self):
        rounds = [rnd(1, [A, B], tokens=1_000_000), rnd(2, [A, B], tokens=1_000_000)]
        out = ledger.render(rounds, conv.decide(rounds))
        assert "| Rounds | 2 |" in out
        assert "2,000,000" in out

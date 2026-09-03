"""
The stopping rule, exhaustively.

This is the part of Phase 3 that can spend money without anyone watching, and
every one of its states is reachable only through a sequence of expensive runs.
So the sequences are constructed here instead: a round is a dict, and the whole
decision surface is exercised for the price of a unit test.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import convergence as conv  # noqa: E402
from rounds import A, B, C, LOW, PRICES, finding, fix, qa, rnd  # noqa: E402


class TestRoundRecord:
    def test_only_blocking_severities_drive_the_comparison(self):
        """
        The loop's goal is "zero BLOCKING findings", not "zero findings". A LOW
        that nobody intends to fix would otherwise stall every run forever: it
        never resolves, so the set never shrinks.
        """
        r = rnd(1, [A, LOW])
        assert len(r["findings"]) == 2
        assert len(r["blocking"]) == 1

    def test_patches_are_attributed_to_fingerprints_at_record_time(self):
        """
        `applied[].finding_id` is F-001 -- a per-run id that means nothing in any
        other round. Resolving it while both artefacts are in hand is what lets
        the ledger say "patched in round 2, gone in round 3".
        """
        r = rnd(1, [A, C], fix(applied=["F-001"]))
        assert r["patched"] == [conv.schema.finding_fingerprint(A)]

    def test_a_patch_naming_an_unknown_finding_is_not_attributed(self):
        """A stale fix result must not credit a round it did not run against."""
        assert rnd(1, [A], fix(applied=["F-099"]))["patched"] == []

    def test_a_failed_round_is_flagged_rather_than_folded_in(self):
        r = rnd(1, [], error="runtime_unavailable")
        assert r["error"] == "runtime_unavailable"

    def test_the_fix_half_is_priced_from_the_same_table_as_the_qa_half(self):
        """
        The fix result carries units and a model name; dollars are computed here.
        Two programs converting units to money independently is how the two
        disagree.
        """
        r = rnd(1, [A], fix(spent=1_000_000))
        assert r["meters"]["fix_usd"] == pytest.approx(1.0)

    def test_a_fix_that_never_ran_is_not_an_unpriced_fix(self):
        r = rnd(1, [A])
        assert r["meters"]["fix_calls"] == 0
        assert r["meters"]["fix_usd"] is None
        assert r["meters"]["fix_tokens_known"] is True


class TestTheGoal:
    def test_zero_blocking_findings_is_a_pass(self):
        assert conv.decide([rnd(1, [])]).state == conv.PASS

    def test_a_pass_may_still_carry_non_blocking_findings(self):
        """
        A LOW is a note for a human, not a reason to pay for another round.
        """
        d = conv.decide([rnd(1, [LOW])])
        assert d.state == conv.PASS
        assert d.ok is True

    def test_pass_is_the_only_successful_stop(self):
        for state in (conv.STALL, conv.REGRESSED, conv.MAX_ROUNDS, conv.INCONCLUSIVE):
            assert conv.succeeded(state) is False
        assert conv.succeeded(conv.PASS) is True

    def test_every_state_but_continue_stops_the_loop(self):
        assert conv.stops(conv.CONTINUE) is False
        for state in (
            conv.PASS, conv.STALL, conv.REGRESSED, conv.INCONCLUSIVE,
            conv.MAX_ROUNDS, conv.TOKEN_BUDGET, conv.WALL_CLOCK, conv.ROUND_TIMEOUT,
        ):
            assert conv.stops(state) is True


class TestTheComparison:
    def test_the_first_round_has_nothing_to_compare_against(self):
        d = conv.decide([rnd(1, [A, B])])
        assert d.state == conv.CONTINUE
        assert "nothing to compare" in d.reason

    def test_a_shrinking_set_continues(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-002"])), rnd(2, [A])]
        assert conv.decide(rounds).state == conv.CONTINUE

    def test_an_identical_set_stalls(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-001"])), rnd(2, [A, B])]
        d = conv.decide(rounds)
        assert d.state == conv.STALL
        assert "same 2 blocking finding(s)" in d.reason

    def test_a_growing_set_is_a_regression_not_a_stall(self):
        """
        PLAN.md says "identical or growing -> stall". A set that GREW contains
        something that was not there before, and naming that separately is what
        tells the reader to look at the diff rather than at the findings.

        Round 1 patched A, so the code round 2 measures differs from the code
        round 1 measured -- the patch is a real suspect. (Without a round-1 fix
        the two sets describe identical code, and growth is just re-measurement
        variance; that case is STALL, not REGRESSED.)
        """
        rounds = [rnd(1, [A], fix(applied=["F-001"])), rnd(2, [A, C])]
        assert conv.decide(rounds).state == conv.REGRESSED

    def test_a_set_that_shrank_while_gaining_a_member_is_a_regression(self):
        """
        THE REFINEMENT, and the reason it exists.

        {A, B, C} -> {A, D} is smaller, so a size-only reading of the plan's rule
        calls it progress and pays for another round -- while the fix agent has
        just traded two defects for a new one it introduced. Size cannot see
        that; the set can.
        """
        D = finding("/analytics", "CRITICAL", "chart crashes on range change", "F-009")
        # Round 1 patched A, B and C: D only exists because that patch ran.
        rounds = [
            rnd(1, [A, B, C], fix(applied=["F-001", "F-002", "F-003"])),
            rnd(2, [A, D]),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.REGRESSED
        assert "the patch is the suspect" in d.reason

    def test_a_finding_keeps_its_identity_when_its_run_id_changes(self):
        """
        The same defect is F-002 in one round and F-001 in the next. If identity
        moved with the id, every round would look like a complete turnover:
        everything "resolved", everything "new", and the loop would report a
        regression on a run where nothing changed.
        """
        renumbered = {**B, "id": "F-001"}
        rounds = [rnd(1, [A, B]), rnd(2, [A, renumbered])]
        assert conv.decide(rounds).state == conv.STALL

    def test_a_severity_change_is_the_same_defect(self):
        """
        Severity is run-to-run noise, not identity: `aggregate_findings` already
        grades one defect reported HIGH by one run and MEDIUM by another as a
        single defect, so the cross-round comparison must not split a defect
        because a later round graded it worse. The loop still stops -- STALL
        stops it -- and the ledger shows both grades.
        """
        escalated = {**A, "severity": "CRITICAL"}
        rounds = [rnd(1, [A]), rnd(2, [escalated])]
        assert conv.decide(rounds).state == conv.STALL


class TestProseIdentityAcrossRounds:
    """Live run 33588304974: the same /voyage crash rephrased round-to-round."""

    S1 = finding(
        "/voyage",
        "HIGH",
        "Voyage History tab crashes with TypeError: Cannot read properties of "
        "undefined (reading 'toFixed')",
        "F-001",
    )
    S1_REPHRASED = finding(
        "/voyage",
        "HIGH",
        "Voyage History tab crashes with TypeError on toFixed, triggering error "
        "boundary",
        "F-001",
    )

    def test_a_rephrased_persistent_defect_is_a_stall_not_a_regression(self):
        """
        The bug run 33588304974 exposed: round 2 reported the same /voyage crash
        round 1 patched, only rephrased, and the exact-fingerprint difference
        read it as a brand-new blocker -- REGRESSED, "the patch is the suspect" --
        when the honest verdict is that the fix did not take.
        """
        rounds = [
            rnd(1, [self.S1], fix(applied=["F-001"])),
            rnd(2, [self.S1_REPHRASED]),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.STALL
        assert "same 1 blocking finding(s)" in d.reason

    def test_prose_matching_still_counts_real_progress(self):
        """
        Tolerance must not over-match: a second blocker that round 2 no longer
        reports stays resolved even when the surviving one was rephrased.
        """
        S2 = finding("/maintenance", "HIGH", "score rings render blank", "F-002")
        rounds = [
            rnd(1, [self.S1, S2], fix(applied=["F-001", "F-002"])),
            rnd(2, [self.S1_REPHRASED]),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.CONTINUE
        assert "resolved 1 of round 1" in d.reason

    def test_a_genuinely_new_blocker_is_still_a_regression(self):
        NEW = finding(
            "/fleet", "CRITICAL", "history tab throws on range change", "F-002"
        )
        rounds = [
            rnd(1, [self.S1], fix(applied=["F-001"])),
            rnd(2, [self.S1_REPHRASED, NEW]),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.REGRESSED
        assert "the patch is the suspect" in d.reason


class TestAPatchIsMeasuredBeforeTheLoopStops:
    """
    The fix the live runs demanded. A round records its finding set BEFORE its
    fix runs, so a STALL or REGRESSED computed from a round's own pre-fix
    findings passes sentence on a patch that has never been measured. Run
    33590568978 applied the correct backend fixes (voyage.ts actual_fuel ->
    actualFuel, maintenance.ts healthScore) in round 2 and was stopped at
    REGRESSED before round 3 could verify them; run 33588304974 did the same.
    A round that patched always earns a verification round.
    """

    S1 = TestProseIdentityAcrossRounds.S1
    S1_REPHRASED = TestProseIdentityAcrossRounds.S1_REPHRASED
    S2 = finding(
        "/maintenance",
        "HIGH",
        "Health score value missing from every equipment health ring on the "
        "Equipment Health tab",
        "F-002",
    )

    def test_a_round_that_patched_is_not_stalled_on_its_own_pre_fix_set(self):
        """
        Run 33588304974's shape: round 1 patched the crash site wrongly, round 2
        measured the SAME crash still present (rephrased) and patched again --
        this time the real backend cause. The old rule stalled here and threw
        round 2's correct fix away. The loop must run round 3 to verify it.
        """
        rounds = [
            rnd(1, [self.S1], fix(applied=["F-001"])),
            rnd(2, [self.S1_REPHRASED], fix(applied=["F-001"])),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.CONTINUE
        assert "patched 1 blocking finding(s)" in d.reason
        assert "measures whether the fixes took" in d.reason

    def test_a_round_that_patched_is_not_called_a_regression_on_growth(self):
        """
        Run 33590568978's shape: round 1's fix agent skipped (nothing patched),
        round 2 re-measured the same code, saw the S-1 crash AND a newly-blocking
        S-2, and patched both. Round 2's growth happened BEFORE its own fix, so
        the fix is not the suspect -- and patching S-2 makes it exactly what
        round 3 must verify.
        """
        rounds = [
            rnd(1, [self.S1]),
            rnd(2, [self.S1_REPHRASED, self.S2], fix(applied=["F-001", "F-002"])),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.CONTINUE
        assert "patched 2 blocking finding(s)" in d.reason

    def test_growth_on_unchanged_code_without_a_patch_is_a_stall_not_a_regression(self):
        """
        The guard for the grown-set case: if round 2 ALSO declined to patch, the
        two sets describe identical code (no fix ever ran), so a blocker only
        round 2 sees is re-measurement variance -- "the patch is the suspect"
        would name a patch that does not exist. Still a stop: nothing has been
        fixed, so the loop cannot converge.
        """
        rounds = [
            rnd(1, [self.S1]),
            rnd(2, [self.S1_REPHRASED, self.S2]),
        ]
        d = conv.decide(rounds)
        assert d.state == conv.STALL
        assert d.state != conv.REGRESSED
        assert "neither round patched" in d.reason
        assert "re-measurement variance" in d.reason


class TestAnIncompleteRoundProvesNothing:
    def test_a_failed_round_is_inconclusive(self):
        rounds = [rnd(1, [A, B]), rnd(2, [], error="runtime_unavailable")]
        d = conv.decide(rounds)
        assert d.state == conv.INCONCLUSIVE
        assert "runtime_unavailable" in d.reason

    def test_a_failed_round_reporting_nothing_is_never_a_pass(self):
        """
        The failure this state exists for. A run that died returns an empty
        findings list, which is byte-for-byte what a healthy application returns
        -- so without this check the loop would end on a green PASS produced by
        an agent that never reached the app. report.py's `unauthenticated` hint
        makes the same point about a single run; across rounds it is worse,
        because the empty set also reads as convergence on the way in.
        """
        rounds = [rnd(1, [A, B]), rnd(2, [], error="unauthenticated")]
        d = conv.decide(rounds)
        assert d.state != conv.PASS
        assert d.ok is False

    def test_a_truncated_round_is_inconclusive_even_though_its_set_shrank(self):
        rounds = [rnd(1, [A, B]), rnd(2, [A], incomplete=True, stop_reason="wall clock")]
        d = conv.decide(rounds)
        assert d.state == conv.INCONCLUSIVE
        assert "wall clock" in d.reason

    def test_a_truncated_round_with_no_findings_is_not_a_pass(self):
        rounds = [rnd(1, [A]), rnd(2, [], incomplete=True)]
        assert conv.decide(rounds).state == conv.INCONCLUSIVE


class TestBackstops:
    def test_the_hard_round_cap_stops_a_run_that_is_still_progressing(self):
        rounds = [rnd(1, [A, B, C]), rnd(2, [A, B]), rnd(3, [A])]
        d = conv.decide(rounds, {"max_rounds": 3})
        assert d.state == conv.MAX_ROUNDS
        assert "still shrinking" in d.reason

    def test_the_cumulative_token_budget_stops_the_next_round(self):
        rounds = [rnd(1, [A, B], fix(spent=60_000), tokens=50_000), rnd(2, [A], tokens=50_000)]
        d = conv.decide(rounds, {"token_budget": 100_000})
        assert d.state == conv.TOKEN_BUDGET
        assert "160,000 tokens" in d.reason

    def test_the_cumulative_wall_clock_stops_the_next_round(self):
        rounds = [rnd(1, [A, B], seconds=400), rnd(2, [A], seconds=400)]
        assert conv.decide(rounds, {"wall_clock_seconds": 600}).state == conv.WALL_CLOCK

    def test_a_round_that_overran_does_not_start_another(self):
        rounds = [rnd(1, [A, B], seconds=100), rnd(2, [A], seconds=9000)]
        d = conv.decide(rounds, {"round_timeout_seconds": 1500})
        assert d.state == conv.ROUND_TIMEOUT
        assert "9000s" in d.reason

    def test_an_unenforceable_token_budget_stops_the_loop(self):
        """
        PLAN.md 1d: a budget checked against a number you do not have is not a
        budget. A fix run that reported no usage leaves the cumulative total a
        floor, so the loop stops rather than continuing on the arithmetic it
        cannot do.
        """
        rounds = [rnd(1, [A, B], fix(applied=["F-001"], budget=False)), rnd(2, [A])]
        d = conv.decide(rounds, {"token_budget": 10_000_000})
        assert d.state == conv.TOKEN_BUDGET
        assert "floor, not a count" in d.reason

    def test_a_backstop_never_overturns_a_pass(self):
        """
        A run that converged on its third round converged. Reporting MAX_ROUNDS
        there would describe a success as a cap breach.
        """
        rounds = [rnd(1, [A, B], seconds=9000), rnd(2, [A], seconds=9000), rnd(3, [], seconds=9000)]
        d = conv.decide(rounds, {"max_rounds": 3, "wall_clock_seconds": 60})
        assert d.state == conv.PASS

    def test_a_backstop_never_hides_a_stall(self):
        """
        The stall is the actionable fact -- it names the findings the agent
        cannot fix. TOKEN_BUDGET would send the reader to raise a cap instead.
        """
        rounds = [rnd(1, [A, B], tokens=999_999), rnd(2, [A, B], tokens=999_999)]
        d = conv.decide(rounds, {"token_budget": 1000})
        assert d.state == conv.STALL

    def test_defaults_apply_when_no_budgets_are_given(self):
        rounds = [rnd(1, [A, B, C]), rnd(2, [A, B]), rnd(3, [A])]
        assert conv.decide(rounds).state == conv.MAX_ROUNDS

    def test_no_rounds_is_a_programming_error_not_a_verdict(self):
        with pytest.raises(ValueError):
            conv.decide([])


class TestTotals:
    def test_tokens_sum_across_both_halves_of_every_round(self):
        rounds = [rnd(1, [A], fix(spent=1000), tokens=5000), rnd(2, [A], fix(spent=2000), tokens=6000)]
        assert conv.totals(rounds)["tokens"] == 14_000

    def test_one_unpriced_meter_makes_the_whole_total_unpriced(self):
        """
        pricing.py's rule, applied to a sum: a dollar total covering two rounds
        out of three reads as the cost of the run. Units stay exact.
        """
        priced = rnd(1, [A], tokens=1000)
        unpriced = conv.round_record(
            2, qa([A], tokens=1000), None, seconds=60, prices={"models": {}, "compute": PRICES["compute"]}
        )
        agg = conv.totals([priced, unpriced])
        assert agg["usd"] is None
        assert agg["tokens"] == 2000

    def test_a_missing_fix_budget_is_recorded_as_unknown_not_zero(self):
        agg = conv.totals([rnd(1, [A], fix(applied=["F-001"], budget=False))])
        assert agg["tokens_known"] is False
        assert agg["unknown_fix_token_rounds"] == 1


class TestReconciliation:
    def test_a_patched_finding_that_went_away_is_fixed(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-002"])), rnd(2, [A])]
        rows = {r["summary"]: r for r in conv.reconcile(rounds)}
        assert rows[B["summary"]]["outcome"] == conv.FIXED
        assert rows[B["summary"]]["patched_in"] == [1]

    def test_a_patched_finding_that_stayed_says_so(self):
        """
        The most useful row in the table: the patch compiled, passed the suite,
        and did not fix the defect -- which usually means the cause was not in
        the file the patch reached.
        """
        rounds = [rnd(1, [A], fix(applied=["F-001"])), rnd(2, [A])]
        assert conv.reconcile(rounds)[0]["outcome"] == conv.PATCH_INEFFECTIVE

    def test_a_patch_applied_in_the_final_round_is_unverified_not_ineffective(self):
        """
        A round records its findings BEFORE its fix runs (QA measures the code
        the PREVIOUS round's patch produced). A finding that round N found and
        patched, where N is the last round, has therefore never been measured
        with its patch in -- the loop owed it a verification round it never
        ran. Live run 33593197606 did exactly this: its round-3 blocker
        (SAVINGS CRITICAL) was patched in round 3 and the loop hit its 3-round
        cap. Calling that patch "did not fix it" would sentence a patch no QA
        ever saw, the overclaim #59 removed from the stopping rule.
        """
        rounds = [
            rnd(1, [A], fix(applied=["F-001"])),
            rnd(2, [B], fix(applied=["F-002"])),
        ]
        rows = {r["summary"]: r for r in conv.reconcile(rounds)}
        assert rows[B["summary"]]["outcome"] == conv.PATCH_UNVERIFIED
        assert rows[B["summary"]]["patched_in"] == [2]
        # Round 1's patch WAS measured (round 2 no longer reports A): fixed.
        assert rows[A["summary"]]["outcome"] == conv.FIXED

    def test_a_finding_that_vanished_without_a_patch_is_not_reproduced(self):
        rounds = [rnd(1, [A, B], fix(applied=["F-001"])), rnd(2, [A])]
        rows = {r["summary"]: r for r in conv.reconcile(rounds)}
        assert rows[B["summary"]]["outcome"] == conv.NOT_REPRODUCED

    def test_a_finding_first_seen_in_a_later_round_is_introduced(self):
        rounds = [rnd(1, [A]), rnd(2, [A, C])]
        rows = {r["summary"]: r for r in conv.reconcile(rounds)}
        assert rows[C["summary"]]["outcome"] == conv.INTRODUCED

    def test_a_finding_nobody_touched_is_outstanding(self):
        rounds = [rnd(1, [A], fix(skipped=[("F-001", "no file matched")])), rnd(2, [A])]
        assert conv.reconcile(rounds)[0]["outcome"] == conv.OUTSTANDING

    def test_by_design_is_never_inferred(self):
        """
        score.py's honesty rule: an unlabelled finding is unlabelled, never
        correct. "By design" and "agent noise" are human judgements, and a
        ledger that guessed them would be marking the agent's homework.
        """
        rows = conv.reconcile([rnd(1, [A])])
        assert rows[0]["label"] is None

    def test_a_human_label_is_carried_through(self):
        fp = conv.schema.finding_fingerprint(A)
        rows = conv.reconcile([rnd(1, [A])], {fp: "by-design"})
        assert rows[0]["label"] == "by-design"

    def test_blocking_rows_sort_above_the_rest(self):
        rows = conv.reconcile([rnd(1, [LOW, A, B])])
        assert [r["severity"] for r in rows] == ["CRITICAL", "HIGH", "LOW"]

    def test_a_rephrased_defect_is_one_row_not_fixed_plus_introduced(self):
        """
        The ledger must agree with the stopping rule: a /voyage crash rephrased
        between rounds is one row whose patch did not take, not "fixed" in round
        1 plus a new "introduced" crash in round 2.
        """
        S1 = finding(
            "/voyage",
            "HIGH",
            "Voyage History tab crashes with TypeError: Cannot read properties "
            "of undefined (reading 'toFixed')",
            "F-001",
        )
        S1R = finding(
            "/voyage",
            "HIGH",
            "Voyage History tab crashes with TypeError on toFixed, triggering "
            "error boundary",
            "F-001",
        )
        rows = conv.reconcile([rnd(1, [S1], fix(applied=["F-001"])), rnd(2, [S1R])])
        assert len(rows) == 1
        assert rows[0]["outcome"] == conv.PATCH_INEFFECTIVE
        assert rows[0]["patched_in"] == [1]
        assert rows[0]["rounds_seen"] == [1, 2]


class TestVerifyReport:
    """The PR one-before/after ledger (`conv.verify_report`, D-2)."""

    def test_a_finding_still_present_is_still_failing(self):
        rows = conv.verify_report(qa([A]), qa([A]))
        assert len(rows) == 1
        assert rows[0]["status"] == conv.STILL_FAILING
        assert rows[0]["fingerprint"] == conv.schema.finding_fingerprint(A)

    def test_absent_with_a_known_fix_is_fixed(self):
        rows = conv.verify_report(qa([A]), qa([]), fix_intervened=True)
        assert len(rows) == 1
        assert rows[0]["status"] == conv.FIXED

    def test_absence_alone_is_not_reproduced_not_fixed(self):
        """
        Silence must not read as fixed. No fix is known to have intervened, so
        the defect is only "not reproduced" -- one clean run does not erase a
        reported defect on its own (the same rule the round ledger's
        NOT_REPRODUCED exists to state).
        """
        rows = conv.verify_report(qa([A]), qa([]))
        assert len(rows) == 1
        assert rows[0]["status"] == conv.NOT_REPRODUCED

    def test_a_rephrased_defect_is_still_failing(self):
        """
        Same identity the round ledger and stopping rule use: page + Dice over
        significant tokens. A blocker the model rephrased between the two runs
        is one row that never went away -- not "fixed" in the prior plus a
        brand-new defect (the P1.3 bug #57 lesson, applied to the PR path).
        """
        rephrased = finding("/fleet", "HIGH", "health score shows NaN on the equipment card", "F-099")
        rows = conv.verify_report(qa([A]), qa([rephrased]))
        assert len(rows) == 1
        assert rows[0]["status"] == conv.STILL_FAILING

    def test_an_error_rereport_cannot_verify_anything(self):
        rows = conv.verify_report(qa([A, C]), qa([A], error="schema_violation"))
        assert [r["status"] for r in rows] == [conv.UNVERIFIED, conv.UNVERIFIED]

    def test_a_truncated_rereport_cannot_verify_anything(self):
        """
        fix #60's rule, restated for the PR path: a re-measurement that stopped
        early did not necessarily revisit the route a prior defect lives on. One
        that happens to re-report A before stopping is no evidence about the
        defects it never got back to -- so everything is UNVERIFIED, even A.
        """
        rows = conv.verify_report(qa([A, C]), qa([A], incomplete=True))
        assert [r["status"] for r in rows] == [conv.UNVERIFIED, conv.UNVERIFIED]

    def test_defects_new_in_the_rereport_are_not_ledger_rows(self):
        """The ledger answers "what happened to what was reported before"."""
        rows = conv.verify_report(qa([A]), qa([A, B]))
        assert len(rows) == 1
        assert rows[0]["status"] == conv.STILL_FAILING

    def test_no_prior_findings_returns_no_rows(self):
        assert conv.verify_report(qa([]), qa([A])) == []

    def test_blocking_rows_sort_above_the_rest(self):
        rows = conv.verify_report(qa([LOW, A, B]), qa([]))
        assert [r["severity"] for r in rows] == ["CRITICAL", "HIGH", "LOW"]

    # --- D-4: fix_intervened as a fingerprint set (the fix agent's applied list)

    def test_fix_intervened_as_a_set_marks_only_members_fixed(self):
        """
        The D-4 fix-PR form: a fix PR reconciles the origin report against its
        patches' applied list. Only an absent prior finding whose exact
        fingerprint the fix addressed may be FIXED; an absent finding the fix
        never touched is NOT_REPRODUCED even though the same fix ran.
        """
        applied = {conv.schema.finding_fingerprint(A)}
        rows = conv.verify_report(qa([A, C]), qa([]), fix_intervened=applied)
        by_fp = {r["fingerprint"]: r["status"] for r in rows}
        assert by_fp[conv.schema.finding_fingerprint(A)] == conv.FIXED
        assert by_fp[conv.schema.finding_fingerprint(C)] == conv.NOT_REPRODUCED

    def test_an_empty_set_behaves_like_no_fix(self):
        rows = conv.verify_report(qa([A]), qa([]), fix_intervened=set())
        assert len(rows) == 1
        assert rows[0]["status"] == conv.NOT_REPRODUCED

    def test_a_member_still_present_is_still_failing_even_when_in_the_set(self):
        """
        Presence in the re-measurement always wins over the applied set -- a
        patch that claimed a finding but left the defect is STILL_FAILING, never
        FIXED.
        """
        applied = {conv.schema.finding_fingerprint(A)}
        rows = conv.verify_report(qa([A]), qa([A]), fix_intervened=applied)
        assert len(rows) == 1
        assert rows[0]["status"] == conv.STILL_FAILING

    def test_a_fingerprint_outside_the_set_is_not_reproduced(self):
        """Absent, fix ran, but this finding was NOT one it addressed."""
        applied = {conv.schema.finding_fingerprint(A)}
        rows = conv.verify_report(qa([C]), qa([]), fix_intervened=applied)
        assert len(rows) == 1
        assert rows[0]["status"] == conv.NOT_REPRODUCED


class TestUnmatchedCurrent:
    """Current findings with no prior counterpart (`conv.unmatched_current`, D-4)."""

    def test_current_only_findings_are_returned(self):
        prior, current = qa([A]), qa([A, B])
        out = conv.unmatched_current(prior, current)
        assert [f["id"] for f in out] == ["F-002"]

    def test_a_rephrased_prior_defect_is_not_unmatched(self):
        """Same Dice identity as the ledger: a rephrase is the same defect."""
        rephrased = finding("/fleet", "HIGH", "health score renders a NaN value", "F-099")
        out = conv.unmatched_current(qa([A]), qa([rephrased]))
        assert out == []

    def test_an_error_rereport_returns_nothing(self):
        """A broken re-run cannot read as 'introduced nothing new'."""
        assert conv.unmatched_current(qa([A]), qa([B], error="schema_violation")) == []

    def test_a_truncated_rereport_returns_nothing(self):
        assert conv.unmatched_current(qa([A]), qa([B], incomplete=True)) == []

    def test_no_prior_returns_all_current(self):
        assert conv.unmatched_current(None, qa([A, B])) == [A, B]

    def test_an_empty_prior_returns_all_current(self):
        assert conv.unmatched_current(qa([]), qa([A])) == [A]

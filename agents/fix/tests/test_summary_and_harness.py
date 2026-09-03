"""
Summary rendering, and the whole loop end to end with the model stubbed.

`TestEndToEnd` is the one that matters: the real committed fixture, a real tree,
every guardrail live, and the only thing faked is Bedrock. It is the closest
thing to the Phase 2 smoke test that can run without credentials.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness as fix_harness  # noqa: E402
import model as fix_model  # noqa: E402
import summary as fix_summary  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "findings-33140664097.json"


@pytest.fixture
def tree(tmp_path):
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "Dashboard.tsx").write_text(
        "export function Dashboard() {\n"
        "  const firstName = user.name.split(' ')[0];\n"
        "  return <h1>{greeting}, Captain {firstName}</h1>;\n"
        "}\n"
    )
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text("greeting Captain\n")
    return tmp_path


class _Bedrock:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": json.dumps(self.payload)}]}},
            "usage": {"inputTokens": 4000, "outputTokens": 300},
            "stopReason": "end_turn",
        }


def _args(**overrides):
    """
    Args for a harness run.

    Severities default to ALL here, deliberately. The fixture's only finding is
    MEDIUM, and these tests are about harness mechanics -- staleness, caps, exit
    codes -- not about the severity filter. Letting them inherit the production
    default would make every one of them silently exercise nothing, which is the
    failure mode a test is least likely to notice. TestSeverityDefault below
    covers the default itself.
    """
    parser = fix_harness.build_parser()
    overrides.setdefault("severities", "CRITICAL,HIGH,MEDIUM,LOW")
    argv = ["--findings", str(FIXTURE)]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None:
            argv += [flag, str(value)]
    return parser.parse_args(argv)


class TestReport:
    def test_a_run_that_patched_nothing_still_renders_a_full_report(self):
        out = fix_summary.render({"applied": [], "skipped": [{"finding_id": "F-1", "reason": "no source"}]})
        assert "No patches applied" in out
        assert "F-1" in out and "no source" in out

    def test_skips_are_a_table_not_a_footnote(self):
        """
        The reviewer's hardest question about an agent PR is what it did NOT do,
        because that is the part the diff cannot show.
        """
        out = fix_summary.render({"applied": [], "skipped": [{"finding_id": "F-2", "reason": "ambiguous"}]})
        assert "### Skipped" in out and "| Finding | Reason |" in out

    def test_excluded_files_are_reported_separately_from_skips(self):
        """
        "Could not find it" and "was not shown it" are different problems with
        different fixes, and only one of them is a bug.
        """
        out = fix_summary.render(
            {"applied": [], "skipped": [], "excluded": [{"path": "a.tsx", "reason": "byte cap"}]}
        )
        assert "Not shown to the model" in out and "byte cap" in out

    def test_pipes_in_a_reason_cannot_break_the_table(self):
        out = fix_summary.render({"applied": [], "skipped": [{"finding_id": "F", "reason": "a | b"}]})
        assert "a \\| b" in out

    def test_the_cost_line_reports_units_and_dollars(self):
        budget = fix_model.Budget(120_000)
        budget.record({"inputTokens": 15_000, "outputTokens": 2_000})
        line = fix_summary.cost_line(budget, fix_model.DEFAULT_MODEL)
        assert "15,000 in / 2,000 out" in line
        assert "1 call(s)" in line
        assert "$" in line

    def test_an_unknown_model_is_unpriced_never_zero(self):
        """
        PRICING.md's honesty rule. A silent $0.00 reads as "this run was free"
        when it means "nobody told me the price".
        """
        budget = fix_model.Budget()
        budget.record({"inputTokens": 10, "outputTokens": 1})
        assert "unpriced" in fix_summary.cost_line(budget, "some.model.nobody.priced")

    def test_applying_nothing_is_not_an_error(self):
        """The prompt encourages honest skips; a run of all skips succeeded."""
        assert fix_summary.exit_code({"applied": [], "skipped": [{"reason": "x"}]}) == 0

    def test_a_harness_failure_is_an_error(self):
        assert fix_summary.exit_code({"applied": [], "error": "budget exhausted"}) == 1


class TestDryRun:
    def test_a_dry_run_makes_no_call_and_writes_nothing(self, tree, monkeypatch, capsys):
        called = []
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: called.append(1))
        before = (tree / "frontend/src/pages/Dashboard.tsx").read_text()

        code = fix_harness.run(_args(repo=tree, dry_run=True))

        assert code == 0
        assert called == []
        assert (tree / "frontend/src/pages/Dashboard.tsx").read_text() == before
        assert "would show" in capsys.readouterr().out

    def test_a_dry_run_still_reports_which_files_were_selected(self, tree, monkeypatch, capsys):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        fix_harness.run(_args(repo=tree, dry_run=True))
        assert "Dashboard.tsx" in capsys.readouterr().out


class TestEndToEnd:
    """The whole loop against the real fixture, with only Bedrock stubbed."""

    def _stub(self, monkeypatch, payload):
        bedrock = _Bedrock(payload)
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: bedrock)
        return bedrock

    def test_a_good_edit_reaches_the_file(self, tree, monkeypatch, tmp_path):
        self._stub(
            monkeypatch,
            {
                "edits": [
                    {
                        "finding_id": "F-001",
                        "file": "frontend/src/pages/Dashboard.tsx",
                        "old_string": "{greeting}, Captain {firstName}",
                        "new_string": "{greeting}, {firstName}",
                        "rationale": "the name already carries the rank",
                    }
                ],
                "skipped": [],
            },
        )
        summary = tmp_path / "summary.md"
        code = fix_harness.run(_args(repo=tree, summary_out=summary))

        assert code == 0
        patched = (tree / "frontend/src/pages/Dashboard.tsx").read_text()
        assert "Captain {firstName}" not in patched
        text = summary.read_text()
        assert "F-001" in text and "the name already carries the rank" in text

    def test_an_edit_outside_the_allow_list_is_refused_end_to_end(self, tree, monkeypatch):
        """
        The exit criterion at the top level: the model asks for Terraform, the
        file exists, the text matches, and nothing is written.
        """
        self._stub(
            monkeypatch,
            {
                "edits": [
                    {
                        "finding_id": "F-001",
                        "file": "infra/main.tf",
                        "old_string": "greeting Captain",
                        "new_string": "",
                        "rationale": "no",
                    }
                ],
                "skipped": [],
            },
        )
        code = fix_harness.run(_args(repo=tree))
        assert (tree / "infra/main.tf").read_text() == "greeting Captain\n"
        assert code == 0

    def test_a_model_skip_is_carried_into_the_summary(self, tree, monkeypatch, tmp_path):
        self._stub(
            monkeypatch,
            {"edits": [], "skipped": [{"finding_id": "F-001", "reason": "two defensible fixes"}]},
        )
        summary = tmp_path / "s.md"
        fix_harness.run(_args(repo=tree, summary_out=summary))
        assert "two defensible fixes" in summary.read_text()

    def test_an_invalid_response_patches_nothing_and_fails(self, tree, monkeypatch):
        self._stub(monkeypatch, {"edits": [{"file": "x"}], "skipped": []})
        before = (tree / "frontend/src/pages/Dashboard.tsx").read_text()
        code = fix_harness.run(_args(repo=tree))
        assert code == 1
        assert (tree / "frontend/src/pages/Dashboard.tsx").read_text() == before

    def test_the_severity_filter_selects_what_is_attempted(self, tree, monkeypatch, capsys):
        """The fixture's only finding is MEDIUM, so HIGH-only attempts nothing."""
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        fix_harness.run(_args(repo=tree, severities="CRITICAL,HIGH"))
        out = capsys.readouterr().out
        assert "No patches applied" in out
        assert "F-001" not in out

    def test_the_machine_readable_result_is_written_when_asked(self, tree, monkeypatch, tmp_path):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        out = tmp_path / "result.json"
        fix_harness.run(_args(repo=tree, dry_run=True, json_out=out))
        assert set(json.loads(out.read_text())) >= {"applied", "skipped", "excluded"}


class TestPartialRunsAreNotFailures:
    """
    The bug this class exists for was reproduced before it was fixed: two
    findings, the first unparseable and the second fine, produced exit code 1
    with a correct patch already written to disk. In the workflow that failed the
    Propose step, which skipped the gate and the commit, so the patch died on a
    runner that was then destroyed. Good work, silently discarded.
    """

    def test_a_run_with_patches_and_failures_still_proceeds(self):
        result = {"applied": [{"path": "a.tsx"}], "errors": ["bad response"]}
        assert fix_summary.exit_code(result) == 0

    def test_nothing_applied_and_something_broke_is_a_failure(self):
        assert fix_summary.exit_code({"applied": [], "errors": ["bad response"]}) == 1

    def test_nothing_applied_and_nothing_broken_is_success(self):
        """"Every finding honestly skipped" is an outcome the prompt encourages."""
        assert fix_summary.exit_code({"applied": [], "errors": []}) == 0

    def test_the_legacy_single_error_key_is_still_honoured(self):
        assert fix_summary.exit_code({"applied": [], "error": "boom"}) == 1

    def test_a_partial_run_says_so_in_the_summary(self):
        """
        A reviewer told only about the patches will assume the findings list was
        fully addressed. The failures are as much a result as the diff.
        """
        out = fix_summary.render(
            {
                "applied": [{"finding_id": "F-1", "path": "a.tsx", "lines": 2, "rationale": "x"}],
                "skipped": [{"finding_id": "F-2", "reason": "invalid response"}],
                "errors": ["invalid response"],
            }
        )
        assert "PARTIAL, not complete" in out
        assert "1 finding(s) failed" in out

    def test_a_clean_run_does_not_claim_to_be_partial(self):
        out = fix_summary.render(
            {
                "applied": [{"finding_id": "F-1", "path": "a.tsx", "lines": 2, "rationale": "x"}],
                "skipped": [],
                "errors": [],
            }
        )
        assert "PARTIAL" not in out


class TestRunLevelCaps:
    """
    The caps bound the PULL REQUEST -- "a human can review it in under ten
    minutes" is a property of the PR, not of any finding inside it. Enforced
    per-finding, five findings under a five-file cap produce a twenty-five-file
    review.
    """

    def _edit(self, path, old, new):
        return {"file": path, "old_string": old, "new_string": new}

    def test_earlier_findings_count_against_a_later_batch(self, tree, monkeypatch):
        import edits as edit_rules
        import paths

        monkeypatch.setattr(paths, "MAX_FILES_TOUCHED", 1)
        planned = edit_rules.plan(
            [self._edit("frontend/src/pages/Dashboard.tsx", "Captain {firstName}", "x")],
            tree,
            applied_files=frozenset({"frontend/src/pages/Other.tsx"}),
        )
        assert planned["batch_error"] is not None
        assert "this run would touch 2 files" in planned["batch_error"]

    def test_earlier_lines_count_against_a_later_batch(self, tree, monkeypatch):
        import edits as edit_rules
        import paths

        monkeypatch.setattr(paths, "MAX_LINES_CHANGED", 3)
        planned = edit_rules.plan(
            [self._edit("frontend/src/pages/Dashboard.tsx", "Captain {firstName}", "x")],
            tree,
            applied_lines=3,
        )
        assert "this run would change" in planned["batch_error"]

    def test_with_no_prior_spend_the_behaviour_is_unchanged(self, tree):
        import edits as edit_rules

        planned = edit_rules.plan(
            [self._edit("frontend/src/pages/Dashboard.tsx", "Captain {firstName}", "x")], tree
        )
        assert planned["batch_error"] is None


class TestFindingsCap:
    def test_the_budget_is_derived_from_the_findings_cap(self):
        """
        Two independently chosen numbers describe an impossible run -- the same
        argument agent.py makes for turns against tokens. The old flat 120,000
        against a measured 29,402 per finding ran out on the fifth finding of a
        five-finding run.
        """
        assert fix_model.DEFAULT_TOKEN_BUDGET == fix_model.token_budget_for(
            fix_model.DEFAULT_MAX_FINDINGS
        )
        needed = fix_model.DEFAULT_MAX_FINDINGS * fix_model.MEASURED_TOKENS_PER_FINDING
        assert fix_model.DEFAULT_TOKEN_BUDGET >= needed

    def test_the_measured_figure_matches_the_observed_run(self):
        """Run 33409654638 spent 29,402 tokens on one finding."""
        assert fix_model.MEASURED_TOKENS_PER_FINDING >= 29_402

    def test_findings_beyond_the_cap_are_reported_not_dropped(self, tree, monkeypatch, tmp_path):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        out = tmp_path / "s.md"
        fix_harness.run(_args(repo=tree, dry_run=True, max_findings=0, summary_out=out))
        assert "beyond --max-findings (0)" in out.read_text()


class TestStaleness:
    """
    A findings JSON outlives the code it describes, and nothing else in the
    harness noticed. Run 33137979741 observed two defects on `qa-corpus-1`;
    replayed against `main` three days later, one was already fixed and the
    other never existed there. The agent produced confident, compiling, wrong
    patches for both.

    The "old_string not found" check in edits.py does NOT cover this. It stops
    an agent that tries the exact stale edit; one that adapts to the code in
    front of it fixes a defect that is not there. So the guard sits above the
    model rather than below it.
    """

    def test_matching_commits_are_fine(self):
        assert fix_harness.staleness("abc123", "abc123") is None

    def test_a_different_commit_is_refused_with_both_shas(self):
        reason = fix_harness.staleness("a" * 40, "b" * 40)
        assert "aaaaaaaaaaaa" in reason and "bbbbbbbbbbbb" in reason
        assert "--allow-stale-findings" in reason

    def test_unstamped_findings_are_allowed(self):
        """
        Refusing them would break every report written before the field existed.
        Present-and-different is the case we have actually been burned by.
        """
        assert fix_harness.staleness(None, "abc123") is None

    def test_a_non_git_checkout_cannot_be_checked(self):
        assert fix_harness.staleness("abc123", None) is None

    def test_head_of_a_non_repo_is_none(self, tmp_path):
        assert fix_harness._head_commit(tmp_path) is None

    def test_a_non_git_checkout_warns_instead_of_staying_silent(
        self, tree, tmp_path, monkeypatch, caplog
    ):
        """
        `staleness()` itself must keep returning None for a non-git checkout --
        refusing would make a directory an unusable replay target. But the run
        has to SAY so: the guard's only value is warning that the report may not
        describe this code, and a silent pass is a guard that never fired.
        """
        # The committed fixtures carry observed_at_commit, so staleness has
        # something to have been unable to check.
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: None)
        out = tmp_path / "s.md"
        parsed = fix_harness.build_parser().parse_args(
            ["--findings", str(FIXTURE), "--repo", str(tree), "--dry-run",
             "--summary-out", str(out)]
        )
        with caplog.at_level(logging.WARNING):
            fix_harness.run(parsed)
        assert any(
            "not a git repository" in r.message for r in caplog.records
        )

    def test_stale_findings_are_all_skipped_and_nothing_is_written(self, tree, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: called.append(1))
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: "b" * 40)
        payload = json.loads(FIXTURE.read_text())
        payload["observed_at_commit"] = "a" * 40
        f = tmp_path / "stale.json"
        f.write_text(json.dumps(payload))
        out = tmp_path / "s.md"

        args = fix_harness.build_parser().parse_args(
            ["--findings", str(f), "--repo", str(tree), "--summary-out", str(out)]
        )
        code = fix_harness.run(args)

        assert called == []
        assert code == 1
        assert "observed at aaaaaaaaaaaa" in out.read_text()

    def test_the_override_lets_a_deliberate_replay_through(self, tree, tmp_path, monkeypatch):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: "b" * 40)
        payload = json.loads(FIXTURE.read_text())
        payload["observed_at_commit"] = "a" * 40
        f = tmp_path / "stale.json"
        f.write_text(json.dumps(payload))
        out = tmp_path / "s.md"

        args = fix_harness.build_parser().parse_args(
            # Severities explicit: this test is about the staleness override,
            # and the fixture's only finding is MEDIUM.
            ["--findings", str(f), "--repo", str(tree), "--dry-run",
             "--allow-stale-findings", "--summary-out", str(out),
             "--severities", "CRITICAL,HIGH,MEDIUM,LOW"]
        )
        fix_harness.run(args)
        assert "would show" in out.read_text()

    def test_both_committed_fixtures_carry_their_provenance(self):
        """
        Backfilled from the runs that produced them, so they are honest about
        being historical -- and so a replay is correctly refused by default.
        """
        for name in ("findings-33140664097.json", "findings-33137979741.json"):
            path = FIXTURE.parent / name
            payload = json.loads(path.read_text())
            assert len(payload.get("observed_at_commit", "")) == 40


class TestUnreadableFindings:
    """
    A findings file the harness cannot read must fail cleanly -- a summary with
    a reason, a machine-readable result, and an exit code -- not a traceback.
    The findings file is machine-generated by the QA harness, so corruption is
    rare; when it happens the run produced nothing, and the reviewer needs the
    reason, not a stack dump.
    """

    def test_malformed_json_is_a_renderable_error(self, tmp_path):
        bad = tmp_path / "findings.json"
        bad.write_text("{not json")
        out = tmp_path / "s.md"
        jout = tmp_path / "result.json"

        args = fix_harness.build_parser().parse_args(
            ["--findings", str(bad), "--repo", str(tmp_path),
             "--summary-out", str(out), "--json-out", str(jout)]
        )
        code = fix_harness.run(args)

        assert code == 1
        assert "not valid JSON" in out.read_text()
        assert "findings.json" in out.read_text()
        assert json.loads(jout.read_text())["errors"]

    def test_a_findings_key_that_is_not_a_list_fails_the_same_way(self, tmp_path):
        # A MISSING 'findings' key defaults to an empty list, which is the
        # honest reading of a run that found nothing. A PRESENT-but-non-list
        # value is a malformed artefact and must fail cleanly.
        wrong_shape = tmp_path / "findings.json"
        wrong_shape.write_text(json.dumps({"findings": {"some": "object"}}))
        out = tmp_path / "s.md"

        args = fix_harness.build_parser().parse_args(
            ["--findings", str(wrong_shape), "--repo", str(tmp_path),
             "--summary-out", str(out)]
        )
        assert fix_harness.run(args) == 1
        assert "findings" in out.read_text()

    def test_a_findings_load_error_carries_the_filename(self, tmp_path):
        bad = tmp_path / "findings.json"
        bad.write_text("nonsense")
        with pytest.raises(fix_harness.FindingsLoadError) as e:
            fix_harness._load_findings(bad)
        assert "findings.json" in str(e.value)

    def test_unreadable_findings_still_report_a_budget(self, tmp_path):
        """
        Same rule as the stale path: every exit writes the token zero down, or
        Phase 3 cannot tell "no tokens" from "no answer".
        """
        bad = tmp_path / "findings.json"
        bad.write_text("nope")
        jout = tmp_path / "result.json"
        args = fix_harness.build_parser().parse_args(
            ["--findings", str(bad), "--repo", str(tmp_path),
             "--json-out", str(jout)]
        )
        fix_harness.run(args)
        assert json.loads(jout.read_text())["budget"]["calls"] == 0


class TestSeverityDefault:
    """
    CRITICAL and HIGH are exactly the QA schema's BLOCKING severities -- the ones
    that make a run FAIL and gate a PR. Attempting those is a principled line
    rather than an arbitrary one, and it is also the cost control: each finding
    costs a model call (~$0.11) and one of five budget slots, so cosmetic LOWs
    can crowd out the defect that matters.
    """

    def test_the_default_is_the_blocking_severities(self):
        parsed = fix_harness.build_parser().parse_args(["--findings", str(FIXTURE)])
        assert parsed.severities == "CRITICAL,HIGH"

    def test_the_default_matches_the_qa_schema_definition_of_blocking(self):
        """
        If the schema's BLOCKING ever changes, this default should move with it
        rather than drift into an unrelated pair of words.
        """
        # Through the loader, not a bare `import schema` -- the venv ships a
        # site-packages module of that name, and this test only ever worked
        # because the QA test session happened to import the right one first.
        qa_schema = fix_harness._qa_schema()

        assert set(fix_harness.DEFAULT_SEVERITIES.split(",")) == set(qa_schema.BLOCKING)

    def test_a_medium_finding_is_not_attempted_by_default(self, tree, monkeypatch, tmp_path):
        """The committed fixture is MEDIUM, so the default must skip it."""
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: None)
        out = tmp_path / "s.md"
        parsed = fix_harness.build_parser().parse_args(
            ["--findings", str(FIXTURE), "--repo", str(tree), "--dry-run",
             "--summary-out", str(out)]
        )
        fix_harness.run(parsed)
        assert "No patches applied" in out.read_text()

    def test_everything_can_still_be_requested(self, tree, monkeypatch, tmp_path):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: None)
        out = tmp_path / "s.md"
        fix_harness.run(_args(repo=tree, dry_run=True, summary_out=out,
                              severities="CRITICAL,HIGH,MEDIUM,LOW"))
        assert "would show" in out.read_text()


class TestTheBudgetBlock:
    """
    The fix half's token meters, in the machine-readable result.

    This is a Phase 3 prerequisite rather than a Phase 2 nicety. The convergence
    loop enforces a CUMULATIVE token budget across rounds, and it can only add up
    what the artefacts carry: the QA half has always written its tokens into the
    findings JSON, while this half rendered them into a markdown line and dropped
    them from the JSON entirely. A loop that could see one half would compare a
    floor against its cap and call it a total -- so the convergence layer refuses
    to continue when the block is missing, and these tests are what stop it being
    missing.
    """

    def _result(self, tree, monkeypatch, tmp_path, **overrides):
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: None)
        out = tmp_path / "result.json"
        fix_harness.run(_args(repo=tree, dry_run=True, json_out=out, **overrides))
        return json.loads(out.read_text())

    def test_the_result_carries_units_and_the_model(self, tree, monkeypatch, tmp_path):
        budget = self._result(tree, monkeypatch, tmp_path)["budget"]
        assert set(budget) >= {
            "model", "calls", "input_tokens", "output_tokens",
            "cache_read", "cache_write", "spent", "token_budget",
        }
        assert budget["model"] == fix_model.DEFAULT_MODEL

    def test_it_carries_no_dollars(self, tree, monkeypatch, tmp_path):
        """
        Units here, money where the price table lives. agent.py states the rule
        for the QA half -- "converting units to money in two places is how the
        two disagree" -- and it holds across programs too.
        """
        budget = self._result(tree, monkeypatch, tmp_path)["budget"]
        assert not any("usd" in key for key in budget)

    def test_a_run_that_spent_nothing_reports_a_zero_not_a_silence(self, tree, monkeypatch, tmp_path):
        """
        A dry run makes no model call. Zero is the true answer, and it has to be
        written down: the convergence loop treats an ABSENT block as "unknown"
        and stops the loop, which would be the wrong response to a round that
        genuinely cost nothing.
        """
        budget = self._result(tree, monkeypatch, tmp_path)["budget"]
        assert budget["calls"] == 0
        assert budget["spent"] == 0

    def test_refused_stale_findings_still_report_a_budget(self, tree, monkeypatch, tmp_path):
        """
        The staleness guard returns early, before any model call. That exit used
        to write a result with no budget at all -- indistinguishable, downstream,
        from a run whose accounting was lost.
        """
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: None)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: "b" * 40)
        payload = json.loads(FIXTURE.read_text())
        payload["observed_at_commit"] = "a" * 40
        findings = tmp_path / "stale.json"
        findings.write_text(json.dumps(payload))
        out = tmp_path / "result.json"
        parsed = fix_harness.build_parser().parse_args(
            ["--findings", str(findings), "--repo", str(tree), "--json-out", str(out),
             "--summary-out", str(tmp_path / "s.md")]
        )
        fix_harness.run(parsed)
        assert json.loads(out.read_text())["budget"]["calls"] == 0

    def test_a_real_call_is_counted(self, tree, monkeypatch, tmp_path):
        bedrock = _Bedrock({
            "edits": [{
                "file": "frontend/src/pages/Dashboard.tsx",
                "old_string": "const firstName = user.name.split(' ')[0];",
                "new_string": "const firstName = user.name?.split(' ')[0] ?? '';",
                "rationale": "guard the split",
            }],
            "skipped": [],
        })
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: bedrock)
        monkeypatch.setattr(fix_harness, "_head_commit", lambda root: None)
        out = tmp_path / "result.json"
        fix_harness.run(_args(repo=tree, json_out=out, summary_out=tmp_path / "s.md"))
        budget = json.loads(out.read_text())["budget"]
        assert budget["calls"] == 1
        assert budget["spent"] == 4300


F_ORIGIN = "/::MEDIUM::dashboard greeting renders 'captain captain' due to duplicate title prefix"


class TestOriginBanner:
    """The D-4 `### Origin` provenance banner in the PR body."""

    def test_a_fix_pr_body_opens_with_the_origin_banner(self):
        result = {
            "applied": [],
            "skipped": [],
            "origin": {
                "repo": "amdhd/vesselAI",
                "pr": "125",
                "findings_key": "reports/pr-125/latest/findings.json",
                "applied_fingerprints": [F_ORIGIN],
            },
        }
        out = fix_summary.render(result)
        # The banner leads the body so a reader of the fix PR is told which QA
        # run it answers before anything else.
        assert out.startswith("### Origin")
        assert "amdhd/vesselAI#125" in out
        assert "reports/pr-125/latest/findings.json" in out
        assert F_ORIGIN in out

    def test_a_run_without_origin_has_no_banner(self):
        out = fix_summary.render({"applied": [], "skipped": []})
        assert not out.startswith("### Origin")
        assert "### Origin" not in out

    def test_a_run_that_patched_nothing_says_so_in_the_banner(self):
        out = fix_summary.render({
            "applied": [], "skipped": [{"finding_id": "F-001", "reason": "no source"}],
            "origin": {"repo": "amdhd/vesselAI", "pr": "125", "findings_key": "k", "applied_fingerprints": []},
        })
        assert "0)" in out and "nothing was patched" in out


class TestOriginSidecar:
    """The D-4 qa-fix-origin.json committed by an origin fix run."""

    def _stub(self, monkeypatch, payload):
        bedrock = _Bedrock(payload)
        monkeypatch.setattr(fix_model, "client", lambda *a, **k: bedrock)
        return bedrock

    def test_an_origin_run_writes_the_sidecar_and_sets_origin(self, tree, monkeypatch, tmp_path):
        """
        End to end: an origin run resolves each applied finding_id to its origin
        fingerprint (against the WHOLE report, before the severity filter) and
        writes the sidecar the fix PR's QA run will reconcile against.
        """
        self._stub(
            monkeypatch,
            {
                "edits": [
                    {
                        "finding_id": "F-001",
                        "file": "frontend/src/pages/Dashboard.tsx",
                        "old_string": "{greeting}, Captain {firstName}",
                        "new_string": "{greeting}, {firstName}",
                        "rationale": "the name already carries the rank",
                    }
                ],
                "skipped": [],
            },
        )
        summary = tmp_path / "summary.md"
        json_out = tmp_path / "result.json"
        code = fix_harness.run(_args(
            repo=tree, summary_out=summary, json_out=json_out,
            origin_pr="125", origin_repo="amdhd/vesselAI",
        ))

        assert code == 0
        sidecar = json.loads((tree / "qa-fix-origin.json").read_text())
        assert sidecar["schema"] == "pipelineguard/fix-origin/v1"
        assert sidecar["origin"] == {"repo": "amdhd/vesselAI", "pr": "125"}
        # The findings key is derived when --origin-findings-key is absent.
        assert sidecar["origin_findings_key"] == "reports/pr-125/latest/findings.json"
        assert sidecar["applied_fingerprints"] == [F_ORIGIN]
        # The machine-readable result carries the same provenance for the summary.
        result = json.loads(json_out.read_text())
        assert result["origin"]["pr"] == "125"
        assert result["origin"]["applied_fingerprints"] == [F_ORIGIN]

    def test_an_origin_run_honours_an_explicit_findings_key(self, tree, monkeypatch, tmp_path):
        self._stub(
            monkeypatch,
            {
                "edits": [
                    {
                        "finding_id": "F-001",
                        "file": "frontend/src/pages/Dashboard.tsx",
                        "old_string": "{greeting}, Captain {firstName}",
                        "new_string": "{greeting}, {firstName}",
                        "rationale": "the name already carries the rank",
                    }
                ],
                "skipped": [],
            },
        )
        fix_harness.run(_args(
            repo=tree, summary_out=tmp_path / "s.md",
            origin_pr="125", origin_repo="amdhd/vesselAI",
            origin_findings_key="reports/pr-125/33782331480/findings.json",
        ))
        sidecar = json.loads((tree / "qa-fix-origin.json").read_text())
        assert sidecar["origin_findings_key"] == "reports/pr-125/33782331480/findings.json"

    def test_plain_runs_leave_no_sidecar(self, tree, monkeypatch, tmp_path):
        """No --origin-pr: existing fixture-dispatch behaviour is unchanged."""
        self._stub(
            monkeypatch,
            {
                "edits": [
                    {
                        "finding_id": "F-001",
                        "file": "frontend/src/pages/Dashboard.tsx",
                        "old_string": "{greeting}, Captain {firstName}",
                        "new_string": "{greeting}, {firstName}",
                        "rationale": "the name already carries the rank",
                    }
                ],
                "skipped": [],
            },
        )
        fix_harness.run(_args(repo=tree, summary_out=tmp_path / "s.md"))
        assert not (tree / "qa-fix-origin.json").exists()

    def test_a_run_that_could_not_read_its_input_writes_no_sidecar(self, tree, tmp_path):
        """Early exits (bad input, stale) happen before the sidecar, so a run
        that will not open a PR never leaves one behind."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        fix_harness.run(_args(repo=tree, findings=bad, origin_pr="125", origin_repo="amdhd/vesselAI"))
        assert not (tree / "qa-fix-origin.json").exists()

    def test_unresolvable_applied_ids_are_dropped_not_emitted(self):
        """
        A patch whose finding_id cannot be resolved against the origin report is
        dropped from the applied set. It can never be called FIXED downstream,
        which is the safe direction.
        """
        fixture_finding = json.loads(FIXTURE.read_text())["findings"][0]
        by_id = {"F-001": fixture_finding}
        fps = fix_harness._applied_fingerprints(
            [
                {"finding_id": "F-001", "path": "a", "lines": 1, "rationale": "yes"},
                {"finding_id": "F-999", "path": "b", "lines": 2, "rationale": "stale id"},
                {"finding_id": "F-001", "path": "c", "lines": 1, "rationale": "duplicate"},
            ],
            by_id,
        )
        assert fps == [F_ORIGIN]

    def test_the_origin_flags_have_safe_defaults(self):
        args = fix_harness.build_parser().parse_args(["--findings", "x.json"])
        assert args.origin_pr == "" and args.origin_repo == "" and args.origin_findings_key == ""

"""
Summary rendering, and the whole loop end to end with the model stubbed.

`TestEndToEnd` is the one that matters: the real committed fixture, a real tree,
every guardrail live, and the only thing faked is Bedrock. It is the closest
thing to the Phase 2 smoke test that can run without credentials.
"""

import json
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
    parser = fix_harness.build_parser()
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

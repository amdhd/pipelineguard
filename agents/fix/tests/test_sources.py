"""
File-selection tests.

The most important test in this file is `TestAgainstTheRealFixture`. Everything
else checks a rule; that one checks the actual input Phase 2's smoke test will
run on -- `page: "/"`, `suspected_source: null` -- which is the combination that
defeats both of the obvious selection strategies.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sources as selector  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "findings-33140664097.json"


@pytest.fixture
def tree(tmp_path):
    """A miniature of the parts of vesselAI selection is allowed to walk."""
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "Dashboard.tsx").write_text(
        "export function Dashboard() {\n"
        "  const firstName = user.name.split(' ')[0];\n"
        "  return <h1>{greeting}, Captain {firstName}</h1>;\n"
        "}\n"
    )
    (pages / "Maintenance.tsx").write_text(
        "export function Maintenance() {\n  return <EquipmentGrid />;\n}\n"
    )
    (pages / "Ports.tsx").write_text("export function Ports() { return <Map />; }\n")

    lib = tmp_path / "backend" / "src" / "lib"
    lib.mkdir(parents=True)
    (lib / "fuelModel.ts").write_text("export const burnRate = 1;\n")

    # Must never be selected: outside the allow-list, and denied respectively.
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text("greeting Captain Dashboard\n")
    angular = tmp_path / "frontend-angular" / "src"
    angular.mkdir(parents=True)
    (angular / "app.component.ts").write_text("greeting Captain Dashboard\n")
    (tmp_path / "frontend" / "src" / ".env.local").write_text("greeting Captain\n")
    return tmp_path


class TestSearchTerms:
    def test_quoted_literals_outrank_plain_words(self):
        terms = selector.search_terms(
            {"summary": "greeting renders 'Captain Captain' on the dashboard", "page": "/"}
        )
        assert terms[0][0] == "Captain Captain"
        assert terms[0][1] == selector.WEIGHT_LITERAL

    def test_the_root_route_contributes_no_term(self):
        """
        "/" matches everything and discriminates nothing -- and it is exactly the
        page the committed fixture carries. An empty segment silently becoming a
        term would make every file score equally.
        """
        terms = selector.search_terms({"summary": "x", "page": "/"})
        assert all(term != "/" and term != "" for term, _ in terms)

    def test_a_real_route_does_contribute_a_term(self):
        terms = dict(
            (t.lower(), w) for t, w in selector.search_terms({"summary": "x", "page": "/maintenance"})
        )
        assert terms.get("maintenance") == selector.WEIGHT_ROUTE

    def test_stopwords_are_dropped(self):
        terms = [t.lower() for t, _ in selector.search_terms({"summary": "the value should be there"})]
        assert "the" not in terms and "should" not in terms

    def test_terms_are_deduplicated_at_the_highest_weight(self):
        """A word that also appears inside a literal is scored as the literal."""
        terms = dict((t.lower(), w) for t, w in selector.search_terms({"summary": "'greeting' greeting"}))
        assert terms["greeting"] == selector.WEIGHT_LITERAL

    def test_terms_are_ordered_deterministically(self):
        finding = {"summary": "Dashboard greeting renders 'Captain Captain'", "page": "/"}
        assert selector.search_terms(finding) == selector.search_terms(finding)


class TestCandidateFiles:
    def test_only_allow_listed_source_is_walked(self, tree):
        found = selector.candidate_files(tree)
        assert "frontend/src/pages/Dashboard.tsx" in found
        assert "backend/src/lib/fuelModel.ts" in found
        assert not any(f.startswith("infra/") for f in found)
        assert not any(f.startswith("frontend-angular/") for f in found)

    def test_denied_files_inside_the_allowed_tree_are_not_walked(self, tree):
        """
        The model is never shown a file it could not have patched. edits.py would
        refuse the proposal anyway; not eliciting it is better than refusing it.
        """
        assert not any(".env" in f for f in selector.candidate_files(tree))

    def test_the_order_is_sorted_not_walk_order(self, tree):
        assert selector.candidate_files(tree) == sorted(selector.candidate_files(tree))


class TestSelection:
    def test_a_valid_suspected_source_wins_outright(self, tree):
        chosen = selector.select(
            {
                "summary": "burn rate is wrong",
                "page": "/voyage",
                "suspected_source": "backend/src/lib/fuelModel.ts",
            },
            tree,
        )
        assert chosen["files"][0]["path"] == "backend/src/lib/fuelModel.ts"
        assert chosen["files"][0]["score"] == selector.WEIGHT_SUSPECTED

    def test_a_denied_suspected_source_is_ignored_not_obeyed(self, tree):
        """
        The QA agent guesses. A guess pointing at Terraform is a guess, not an
        instruction, and honouring it would let a nullable free-text field from
        one model steer another model's write path.
        """
        chosen = selector.select(
            {"summary": "greeting is wrong", "page": "/", "suspected_source": "infra/main.tf"},
            tree,
        )
        assert all(f["path"] != "infra/main.tf" for f in chosen["files"])

    def test_a_suspected_source_that_does_not_exist_falls_back_to_search(self, tree):
        chosen = selector.select(
            {
                "summary": "Dashboard greeting renders 'Captain Captain'",
                "page": "/",
                "suspected_source": "frontend/src/pages/Gone.tsx",
            },
            tree,
        )
        assert chosen["files"][0]["path"] == "frontend/src/pages/Dashboard.tsx"

    def test_nothing_matching_is_reported_rather_than_guessed(self, tree):
        chosen = selector.select(
            {"summary": "zzzqqq wobblefish telemetry", "page": "/", "suspected_source": None}, tree
        )
        assert chosen["files"] == []
        assert "no allow-listed source matched" in chosen["reason"]

    def test_the_no_match_reason_names_the_terms_it_tried(self, tree):
        """
        Distinguishes "the summary was vague" from "the file is outside the
        allow-list", which are different problems with different fixes.
        """
        chosen = selector.select({"summary": "zzzqqq wobblefish", "page": "/"}, tree)
        assert "terms tried:" in chosen["reason"]

    def test_selection_is_repeatable(self, tree):
        finding = {"summary": "Dashboard greeting renders 'Captain Captain'", "page": "/"}
        first = [f["path"] for f in selector.select(finding, tree)["files"]]
        second = [f["path"] for f in selector.select(finding, tree)["files"]]
        assert first == second


class TestCaps:
    def test_the_file_cap_excludes_with_a_reason(self, tree, monkeypatch):
        monkeypatch.setattr(selector, "MAX_FILES", 1)
        chosen = selector.select({"summary": "export function", "page": "/"}, tree)
        assert len(chosen["files"]) == 1
        assert chosen["excluded"]
        assert "file cap" in chosen["excluded"][0]["reason"]

    def test_the_byte_cap_excludes_with_a_reason(self, tree, monkeypatch):
        monkeypatch.setattr(selector, "MAX_BYTES", 10)
        chosen = selector.select({"summary": "export function", "page": "/"}, tree)
        assert any("byte cap" in e["reason"] for e in chosen["excluded"])

    def test_an_oversized_file_is_skipped_entirely(self, tree, monkeypatch):
        monkeypatch.setattr(selector, "MAX_FILE_BYTES", 10)
        chosen = selector.select({"summary": "export function", "page": "/"}, tree)
        assert chosen["files"] == []


class TestAgainstTheRealFixture:
    """
    The committed findings JSON from run 33140664097 -- the exact input the Phase
    2 smoke test will use. It has `page: "/"` and `suspected_source: null`, so
    neither the route nor the agent's guess narrows anything. If selection cannot
    find the file from the summary text alone, the smoke test fails at step one.
    """

    def _finding(self):
        return json.loads(FIXTURE.read_text())["findings"][0]

    def test_the_fixture_still_has_the_shape_these_tests_assume(self):
        finding = self._finding()
        assert finding["page"] == "/"
        assert finding["suspected_source"] is None

    def test_the_greeting_source_is_located_from_the_summary_alone(self, tree):
        chosen = selector.select(self._finding(), tree)
        assert chosen["reason"] is None
        assert chosen["files"][0]["path"] == "frontend/src/pages/Dashboard.tsx"

    def test_nothing_denied_is_ever_offered_to_the_model(self, tree):
        chosen = selector.select(self._finding(), tree)
        offered = {f["path"] for f in chosen["files"]}
        assert not any(p.startswith(("infra/", "frontend-angular/")) for p in offered)
        assert not any(".env" in p for p in offered)


class TestPathScoring:
    """
    The correction the real repo forced. Quoted UI text is a strong signal for a
    STRING and can point at the wrong file for a TEMPLATE: the fixture quotes the
    seeded name "Captain Ahmad Fauzi", which is data and lives in the seed, while
    "Captain Captain" is rendered output that appears in no source file at all.
    """

    def test_a_filename_match_outweighs_a_body_match(self):
        terms = [("Dashboard", selector.WEIGHT_WORD)]
        in_name = selector.score_file("nothing here", terms, "frontend/src/pages/DashboardPage.tsx")
        in_body = selector.score_file("Dashboard", terms, "backend/src/routes/auth.ts")
        assert in_name > in_body

    def test_the_path_bonus_is_a_multiplier_not_a_flat_bump(self):
        """A filename match on a high-weight literal must outrank one on a stopword-ish term."""
        strong = selector.score_file("", [("Greeting", selector.WEIGHT_LITERAL)], "src/Greeting.tsx")
        weak = selector.score_file("", [("Greeting", selector.WEIGHT_WORD)], "src/Greeting.tsx")
        assert strong > weak

    def test_a_file_matching_neither_scores_zero(self):
        assert selector.score_file("x", [("Dashboard", 1)], "a/b.ts") == 0

    def test_the_bonus_is_kept_conservative(self):
        """
        Tuned against exactly one finding. This repo's own evidence notes say a
        single run is not a measurement; the same applies to a single fixture.
        """
        assert selector.PATH_BONUS <= 3


class TestQuoteExtraction:
    def test_an_apostrophe_does_not_open_a_quoted_term(self):
        """
        Before the lookarounds, "the user's stored display name is 'Captain
        Ahmad Fauzi'" matched from `user'` to the next apostrophe -- yielding
        "s stored display name is " and DISCARDING both real literals. It
        extracted nothing but garbage, and selection was working in spite of it.
        """
        terms = [
            t
            for t, _ in selector.search_terms(
                {"summary": "the user's display name is 'Captain Ahmad Fauzi' here"}
            )
        ]
        assert "Captain Ahmad Fauzi" in terms
        assert not any(t.startswith("s display name") for t in terms)

    def test_double_quotes_and_backticks_still_work(self):
        terms = [t for t, _ in selector.search_terms({"summary": 'shows "NaN kg" in `fuelBurn`'})]
        assert "NaN kg" in terms and "fuelBurn" in terms

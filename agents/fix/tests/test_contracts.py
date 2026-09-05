"""
Repo-declared API contract context.

The test that carries the most weight here is `TestTheHundredAndThirtyShape`,
which reconstructs the vesselAI PR #130 selection in miniature. Everything else
checks a rule; that one checks the thing the feature exists for -- and in
particular that the contract pool does NOT come out of the editable budget,
which is the mistake that would have made the manifest worse than nothing.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contracts  # noqa: E402
import edits as edit_rules  # noqa: E402
import prompt as fix_prompt  # noqa: E402
import sources as selector  # noqa: E402


def write_manifest(root: Path, payload: dict) -> None:
    (root / contracts.MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    """
    A miniature of vesselAI's voyage feature, with #130's defect planted.

    The route sends `actual_fuel`; the type declares `actualFuel: number`
    non-null; the formatter calls .toFixed() on it unguarded. That is the whole
    bug, and it is only visible if you can see all three files at once -- which
    is the point being tested.
    """
    lib = tmp_path / "frontend" / "src" / "lib"
    lib.mkdir(parents=True)
    (lib / "types.ts").write_text(
        "export interface Vessel {\n  id: string\n}\n\n"
        "export interface VoyageHistoryRecord {\n"
        "  id: string\n  plannedFuel: number\n  actualFuel: number\n}\n"
    )
    (lib / "utils.ts").write_text(
        "export function formatDate(d: string): string {\n  return d\n}\n\n"
        "export function formatFuel(mt: number): string {\n"
        "  return `${mt.toFixed(1)} MT`\n}\n"
    )

    modules = tmp_path / "frontend" / "src" / "modules" / "voyage"
    modules.mkdir(parents=True)
    (modules / "VoyageHistory.tsx").write_text(
        "export default function VoyageHistory() {\n"
        "  return <td>{formatFuel(voyage.actualFuel)}</td>\n}\n"
    )
    # ENOUGH NOISE TO MAKE THE CAP BIND, which is the only condition under
    # which this feature does anything. In the real #130 run, 131 files were
    # withheld at `file cap (8)` and the three contract files sat at score 6,
    # under six same-directory siblings. A fixture small enough for everything
    # to fit would test the opposite of the situation being fixed -- and did,
    # on the first run of these tests: types.ts ranked in on its own and the
    # precedence rule then correctly kept it editable, failing the assertion
    # for the right reason.
    #
    # These live under modules/voyage/ so they collect PATH_BONUS on the route
    # term, exactly as the real siblings did.
    # SIX of them, and they do not mention toFixed. That reproduces the real
    # ordering rather than merely producing pressure: VoyageHistory.tsx ranked
    # FIRST in the #130 run and backend/src/routes/voyage.ts second, with six
    # siblings filling the rest of the cap. Noise strong enough to outrank the
    # crashing component would test a situation that never happened -- and did,
    # on the second run of these tests.
    for noise in (
        "RouteOptimizer", "SpeedOptimizer", "WeatherPanel",
        "AgentPlanner", "VoyagePage", "RouteMap",
    ):
        (modules / f"{noise}.tsx").write_text(
            f"// voyage fuel history\nexport function {noise}() {{ return null }}\n"
        )

    routes = tmp_path / "backend" / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "voyage.ts").write_text(
        "router.get('/history/:id', (req, res) => {\n"
        "  res.json(voyages.map(v => ({\n"
        "    plannedFuel: v.plannedFuel,\n"
        "    actual_fuel: v.actualFuel ?? v.plannedFuel,\n"
        "  })))\n})\n"
    )
    return tmp_path


VOYAGE_MANIFEST = {
    "version": 1,
    "features": [
        {
            "id": "voyage",
            "match": {"page_prefix": "/voyage"},
            "context": [
                {
                    "path": "frontend/src/lib/types.ts",
                    "anchor": "export interface VoyageHistoryRecord",
                    "span": 6,
                },
                {
                    "path": "frontend/src/lib/utils.ts",
                    "anchor": "export function formatFuel",
                    "span": 3,
                },
            ],
        }
    ],
}

FINDING = {
    "id": "F-001",
    "severity": "HIGH",
    "page": "/voyage",
    "summary": "Voyage History tab crashes the view with a TypeError on 'toFixed'",
}


class TestLoading:
    def test_absent_manifest_is_silent(self, tree):
        """
        Most repos will never have one. A missing manifest must not produce a
        warning, or every run in every other repo carries noise forever.
        """
        manifest, warnings = contracts.load(tree)
        assert manifest is None
        assert warnings == []

    def test_malformed_manifest_warns_but_does_not_raise(self, tree):
        """
        The manifest is an optimisation. A typo in it must not take down a run
        that would otherwise produce a correct fix from ordinary selection.
        """
        (tree / contracts.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        manifest, warnings = contracts.load(tree)
        assert manifest is None
        assert "could not be parsed" in warnings[0]

    def test_unknown_version_is_refused_not_guessed(self, tree):
        """
        A harness pinned to an older SHA reading a newer manifest should say so
        rather than improvise over a schema it does not know.
        """
        write_manifest(tree, {"version": 99, "features": []})
        manifest, warnings = contracts.load(tree)
        assert manifest is None
        assert "version 99" in warnings[0]


class TestMatching:
    def test_page_prefix_matches(self, tree):
        write_manifest(tree, VOYAGE_MANIFEST)
        manifest, _ = contracts.load(tree)
        assert contracts.match_feature(FINDING, manifest)["id"] == "voyage"

    def test_other_page_does_not_match(self, tree):
        write_manifest(tree, VOYAGE_MANIFEST)
        manifest, _ = contracts.load(tree)
        assert contracts.match_feature({**FINDING, "page": "/ports"}, manifest) is None

    def test_root_page_prefix_is_refused(self, tree):
        """
        "/" prefixes every route and discriminates nothing. sources.search_terms
        already refuses it as a term for the same reason; a feature that claimed
        it would silently own every finding in the repo.
        """
        write_manifest(
            tree,
            {"version": 1, "features": [{"id": "all", "match": {"page_prefix": "/"}, "context": []}]},
        )
        manifest, _ = contracts.load(tree)
        assert contracts.match_feature({**FINDING, "page": "/ports"}, manifest) is None

    def test_terms_match_when_the_route_cannot(self, tree):
        """
        The fallback that matters. A dashboard finding carries page "/", so
        prefix matching is useless exactly where routing information is weakest.
        """
        write_manifest(
            tree,
            {
                "version": 1,
                "features": [
                    {"id": "ets", "match": {"terms": ["ets", "compliance"]}, "context": []}
                ],
            },
        )
        manifest, _ = contracts.load(tree)
        finding = {"page": "/", "summary": "ETS compliance status differs between views"}
        assert contracts.match_feature(finding, manifest)["id"] == "ets"

    def test_longer_prefix_wins(self, tree):
        """
        So a repo can declare a broad feature and a narrow one without them
        fighting over the same finding.
        """
        write_manifest(
            tree,
            {
                "version": 1,
                "features": [
                    {"id": "broad", "match": {"page_prefix": "/voy"}, "context": []},
                    {"id": "narrow", "match": {"page_prefix": "/voyage"}, "context": []},
                ],
            },
        )
        manifest, _ = contracts.load(tree)
        assert contracts.match_feature(FINDING, manifest)["id"] == "narrow"


class TestSlicing:
    def test_anchor_cuts_the_declared_region(self, tree):
        write_manifest(tree, VOYAGE_MANIFEST)
        resolved = contracts.for_finding(FINDING, tree)
        types_block = next(e for e in resolved["readonly"] if e["path"].endswith("types.ts"))
        assert "VoyageHistoryRecord" in types_block["text"]
        # The unrelated interface above it is not carried along.
        assert "interface Vessel" not in types_block["text"]

    def test_missing_anchor_shows_whole_file_and_warns(self, tree):
        """
        THE FAILURE THAT SHAPED THIS MODULE. A slice that has drifted onto the
        wrong lines is worse than no slice: it is handed to the model as
        authoritative fact about the contract, which is the exact class of
        confident lie the feature exists to prevent. So a stale anchor degrades
        to "too much context", never to "confidently wrong context".
        """
        broken = json.loads(json.dumps(VOYAGE_MANIFEST))
        broken["features"][0]["context"][0]["anchor"] = "export interface Renamed"
        write_manifest(tree, broken)
        resolved = contracts.for_finding(FINDING, tree)
        types_block = next(e for e in resolved["readonly"] if e["path"].endswith("types.ts"))
        assert "interface Vessel" in types_block["text"]  # the whole file
        assert any(w.startswith("manifest_stale") for w in resolved["warnings"])

    def test_line_range_past_end_of_file_warns(self, tree):
        broken = {
            "version": 1,
            "features": [
                {
                    "id": "voyage",
                    "match": {"page_prefix": "/voyage"},
                    "context": [{"path": "frontend/src/lib/types.ts", "lines": [1, 9999]}],
                }
            ],
        }
        write_manifest(tree, broken)
        resolved = contracts.for_finding(FINDING, tree)
        assert any(w.startswith("manifest_stale") for w in resolved["warnings"])

    def test_deleted_file_warns_rather_than_failing(self, tree):
        broken = json.loads(json.dumps(VOYAGE_MANIFEST))
        broken["features"][0]["context"].append({"path": "frontend/src/lib/gone.ts"})
        write_manifest(tree, broken)
        resolved = contracts.for_finding(FINDING, tree)
        assert any("gone.ts does not exist" in w for w in resolved["warnings"])


class TestTheAllowList:
    def test_manifest_cannot_smuggle_a_denied_path(self, tree):
        """
        A manifest is repo-declared, which makes it trusted-ish, not trusted. It
        must not become a way to put infra in front of a code-writing model --
        selection walks the same allow-list everywhere else, and this is not an
        exception to that.
        """
        write_manifest(
            tree,
            {
                "version": 1,
                "features": [
                    {
                        "id": "voyage",
                        "match": {"page_prefix": "/voyage"},
                        "context": [{"path": "infra/main.tf"}],
                    }
                ],
            },
        )
        resolved = contracts.for_finding(FINDING, tree)
        assert resolved["readonly"] == []
        assert any("not allow-listed" in w for w in resolved["warnings"])


class TestTheHundredAndThirtyShape:
    """
    The #130 selection, reconstructed. See contracts.py's module docstring and
    vesselAI's d4-demo/BASELINE.md for the measurement these assert against.
    """

    def test_contract_files_arrive_without_a_manifest_being_present(self, tree):
        """The baseline: no manifest, and the contract files are simply absent."""
        selection = selector.select(FINDING, tree)
        shown = {f["path"] for f in selection["files"]}
        assert "frontend/src/lib/types.ts" not in shown
        assert selection["contract"] == []

    def test_manifest_adds_them_as_read_only(self, tree):
        write_manifest(tree, VOYAGE_MANIFEST)
        selection = selector.select(FINDING, tree)
        contract_paths = {e["path"] for e in selection["contract"]}
        assert contract_paths == {"frontend/src/lib/types.ts", "frontend/src/lib/utils.ts"}
        assert selection["contract_feature"] == "voyage"

    def test_the_editable_set_is_not_shrunk_by_the_contract(self, tree):
        """
        THE MISTAKE THIS FEATURE MUST NOT MAKE. If contract context competed
        inside MAX_FILES, declaring files for a feature would take slots from
        the editable set -- and in the real #130 run, two of those slots held
        the only correct selections it made. A manifest that made selection
        worse would be worse than no manifest.
        """
        before = selector.select(FINDING, tree)
        write_manifest(tree, VOYAGE_MANIFEST)
        after = selector.select(FINDING, tree)
        assert [f["path"] for f in after["files"]] == [f["path"] for f in before["files"]]

    def test_editable_true_forces_a_path_into_the_editable_set(self, tree):
        """
        The trap that shaped the flag: backend/src/routes/voyage.ts is both the
        API boundary and where #130's correct fix belonged. Declaring it must
        make it MORE available, never unpatchable.
        """
        manifest = json.loads(json.dumps(VOYAGE_MANIFEST))
        manifest["features"][0]["context"].append(
            {"path": "backend/src/routes/voyage.ts", "editable": True}
        )
        write_manifest(tree, manifest)
        selection = selector.select(FINDING, tree)
        assert "backend/src/routes/voyage.ts" in {f["path"] for f in selection["files"]}
        assert "backend/src/routes/voyage.ts" not in {e["path"] for e in selection["contract"]}

    def test_editable_selection_wins_over_read_only(self, tree):
        """
        Precedence, stated as a test because getting it backwards is the
        catastrophic direction: a file that earned an editable slot must never
        be demoted to reference by a manifest entry.
        """
        manifest = json.loads(json.dumps(VOYAGE_MANIFEST))
        manifest["features"][0]["context"].append(
            {"path": "frontend/src/modules/voyage/VoyageHistory.tsx"}
        )
        write_manifest(tree, manifest)
        selection = selector.select(FINDING, tree)
        history = "frontend/src/modules/voyage/VoyageHistory.tsx"
        assert history in {f["path"] for f in selection["files"]}
        assert history not in {e["path"] for e in selection["contract"]}


class TestBudget:
    def test_contract_file_cap_is_recorded_as_excluded(self, tree):
        lib = tree / "frontend" / "src" / "lib"
        entries = []
        for i in range(selector.MAX_CONTEXT_FILES + 2):
            (lib / f"c{i}.ts").write_text(f"export const c{i} = {i}\n")
            entries.append({"path": f"frontend/src/lib/c{i}.ts"})
        write_manifest(
            tree,
            {
                "version": 1,
                "features": [
                    {"id": "voyage", "match": {"page_prefix": "/voyage"}, "context": entries}
                ],
            },
        )
        selection = selector.select(FINDING, tree)
        assert len(selection["contract"]) == selector.MAX_CONTEXT_FILES
        reasons = [e["reason"] for e in selection["excluded"]]
        assert any("contract file cap" in r for r in reasons), reasons

    def test_contract_byte_cap_is_recorded_as_excluded(self, tree):
        lib = tree / "frontend" / "src" / "lib"
        (lib / "huge.ts").write_text("x" * (selector.MAX_CONTEXT_BYTES + 10))
        write_manifest(
            tree,
            {
                "version": 1,
                "features": [
                    {
                        "id": "voyage",
                        "match": {"page_prefix": "/voyage"},
                        "context": [{"path": "frontend/src/lib/huge.ts"}],
                    }
                ],
            },
        )
        selection = selector.select(FINDING, tree)
        assert selection["contract"] == []
        assert any("contract byte cap" in e["reason"] for e in selection["excluded"])


class TestTheReadOnlyGuard:
    def test_an_edit_naming_contract_context_is_rejected(self, tree):
        """
        Enforced here, not merely requested in the prompt. The failure it guards
        is plausible and silent: shown a type that disagrees with the wire, a
        model can quiet the type-checker by editing the TYPE -- leaving the
        crash where it was and the declaration now lying the other way.
        """
        planned = edit_rules.plan(
            [
                {
                    "finding_id": "F-001",
                    "file": "frontend/src/lib/types.ts",
                    "old_string": "actualFuel: number",
                    "new_string": "actual_fuel: number",
                    "rationale": "make the type match the response",
                }
            ],
            tree,
            read_only=frozenset({"frontend/src/lib/types.ts"}),
        )
        assert planned["apply"] == []
        assert "read-only API contract context" in planned["skip"][0]["reason"]

    def test_an_ordinary_edit_still_applies(self, tree):
        """The guard must not be a blanket refusal."""
        planned = edit_rules.plan(
            [
                {
                    "finding_id": "F-001",
                    "file": "backend/src/routes/voyage.ts",
                    "old_string": "actual_fuel:",
                    "new_string": "actualFuel:",
                    "rationale": "send the key the client reads",
                }
            ],
            tree,
            read_only=frozenset({"frontend/src/lib/types.ts"}),
        )
        assert len(planned["apply"]) == 1


class TestThePrompt:
    def test_contract_section_is_labelled_read_only_and_comes_first(self, tree):
        """
        Ordering is deliberate: the declared shape of the data should be read
        before the code that consumes it. That is the difference between "this
        key looks odd" and "this key contradicts the type two files away".
        """
        message = fix_prompt.build(
            FINDING,
            [{"path": "backend/src/routes/voyage.ts", "text": "actual_fuel: 1"}],
            contract=[{"path": "frontend/src/lib/types.ts", "text": "actualFuel: number"}],
        )
        assert "READ ONLY" in message
        assert message.index("API contract context") < message.index("The source you may change")

    def test_no_contract_section_when_there_is_no_manifest(self, tree):
        """Every other repo's prompt must be byte-identical to before."""
        message = fix_prompt.build(
            FINDING, [{"path": "backend/src/routes/voyage.ts", "text": "actual_fuel: 1"}]
        )
        assert "API contract context" not in message

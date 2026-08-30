"""
Edit-applier tests.

The reference implementation's failure mode was a patch tool doing its best with
fuzzy context. These tests pin the opposite behaviour: an edit that does not
match exactly, or matches twice, is refused with a reason -- never guessed at.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import edits  # noqa: E402
import paths  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A tree shaped like the parts of vesselAI the agent may touch."""
    src = tmp_path / "frontend" / "src" / "pages"
    src.mkdir(parents=True)
    (src / "Dashboard.tsx").write_text(
        "export function Dashboard() {\n"
        "  const firstName = user.name.split(' ')[0];\n"
        "  return <h1>{greeting}, Captain {firstName}</h1>;\n"
        "}\n"
    )
    (src / "Repeated.tsx").write_text("const a = 1;\nconst a = 1;\n")
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "main.tf").write_text('resource "aws_s3_bucket" "x" {}\n')
    return tmp_path


def _edit(file, old, new):
    return {"file": file, "old_string": old, "new_string": new}


class TestShapeChecks:
    def test_a_well_formed_edit_passes(self):
        assert edits.check_edit(_edit("frontend/src/App.tsx", "a", "b"), 0) is None

    @pytest.mark.parametrize("missing", ["file", "old_string", "new_string"])
    def test_a_missing_key_is_named(self, missing):
        edit = _edit("frontend/src/App.tsx", "a", "b")
        del edit[missing]
        assert missing in edits.check_edit(edit, 0)

    def test_an_empty_old_string_is_refused(self):
        """
        An empty old_string would turn this into a file-creation tool. The set of
        writable files must not be able to grow -- an agent that cannot create a
        file cannot create one the allow-list was never asked about.
        """
        reason = edits.check_edit(_edit("frontend/src/App.tsx", "", "b"), 0)
        assert "cannot create content" in reason

    def test_a_no_op_edit_is_refused(self):
        assert "no-op" in edits.check_edit(_edit("frontend/src/App.tsx", "a", "a"), 0)

    def test_the_allow_list_is_consulted_by_the_shape_check(self):
        reason = edits.check_edit(_edit("infra/main.tf", "a", "b"), 0)
        assert reason is not None and "allow-list" in reason


class TestPlanning:
    def test_a_matching_edit_is_planned_and_written(self, repo):
        edit = _edit(
            "frontend/src/pages/Dashboard.tsx",
            "{greeting}, Captain {firstName}",
            "{greeting}, {firstName}",
        )
        planned = edits.plan([edit], repo)
        assert planned["batch_error"] is None
        assert len(planned["apply"]) == 1

        edits.write(planned)
        assert "Captain {firstName}" not in (
            repo / "frontend/src/pages/Dashboard.tsx"
        ).read_text()

    def test_a_missing_old_string_is_skipped_with_a_reason(self, repo):
        planned = edits.plan(
            [_edit("frontend/src/pages/Dashboard.tsx", "text that is not there", "x")], repo
        )
        assert planned["apply"] == []
        assert "old_string not found" in planned["skip"][0]["reason"]

    def test_an_ambiguous_old_string_is_skipped_rather_than_guessed(self, repo):
        """
        Two matches is the case a fuzzy patch tool silently gets wrong. Refusing
        it, and saying how many times it matched, is the whole reason this
        package applies structured edits instead of diffs.
        """
        planned = edits.plan([_edit("frontend/src/pages/Repeated.tsx", "const a = 1;", "const a = 2;")], repo)
        assert planned["apply"] == []
        reason = planned["skip"][0]["reason"]
        assert "appears 2 times" in reason and "more surrounding context" in reason

    def test_a_missing_file_is_skipped_with_a_reason(self, repo):
        planned = edits.plan([_edit("frontend/src/pages/Nope.tsx", "a", "b")], repo)
        assert "file does not exist" in planned["skip"][0]["reason"]

    def test_a_denied_path_is_never_written_even_when_it_would_match(self, repo):
        """
        The exit criterion at the applier level: the text matches, the file is
        right there, and nothing is written.
        """
        planned = edits.plan(
            [_edit("infra/main.tf", 'resource "aws_s3_bucket" "x" {}', "")], repo
        )
        assert planned["apply"] == []
        edits.write(planned)
        assert 'resource "aws_s3_bucket" "x" {}' in (repo / "infra/main.tf").read_text()

    def test_a_symlink_to_a_denied_path_inside_the_repo_is_refused(self, repo):
        """
        The case the allow-list structurally cannot catch, and the one the
        obvious containment check misses: this link stays INSIDE the repo root,
        so "is it under root?" says yes. What matters is where it LANDS.
        """
        link = repo / "frontend" / "src" / "pages" / "escape.tsx"
        link.symlink_to(repo / "infra" / "main.tf")
        planned = edits.plan(
            [_edit("frontend/src/pages/escape.tsx", 'resource "aws_s3_bucket" "x" {}', "")], repo
        )
        assert planned["apply"] == []
        assert "resolves to infra/main.tf" in planned["skip"][0]["reason"]

        edits.write(planned)
        assert 'resource "aws_s3_bucket" "x" {}' in (repo / "infra/main.tf").read_text()

    def test_a_symlink_out_of_the_repo_entirely_is_refused(self, repo, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret\n")
        link = repo / "frontend" / "src" / "pages" / "out.tsx"
        link.symlink_to(outside)
        planned = edits.plan([_edit("frontend/src/pages/out.tsx", "secret", "x")], repo)
        assert planned["apply"] == []
        assert "outside the repo" in planned["skip"][0]["reason"]


class TestBatchIsAllOrNothing:
    def test_breaching_the_line_cap_writes_nothing(self, repo, monkeypatch):
        monkeypatch.setattr(paths, "MAX_LINES_CHANGED", 2)
        target = repo / "frontend/src/pages/Dashboard.tsx"
        before = target.read_text()
        planned = edits.plan(
            [
                _edit(
                    "frontend/src/pages/Dashboard.tsx",
                    "  const firstName = user.name.split(' ')[0];\n"
                    "  return <h1>{greeting}, Captain {firstName}</h1>;\n",
                    "  const firstName = displayName(user);\n"
                    "  return <h1>{greeting}, {firstName}</h1>;\n",
                )
            ],
            repo,
        )
        assert planned["batch_error"] is not None
        assert planned["apply"] == []
        edits.write(planned)
        assert target.read_text() == before

    def test_the_batch_reason_is_carried_onto_every_skipped_edit(self, repo, monkeypatch):
        """
        A reviewer reading the summary must see why THIS edit did not land, not
        have to infer it from a separate line about the batch.
        """
        monkeypatch.setattr(paths, "MAX_LINES_CHANGED", 1)
        planned = edits.plan(
            [_edit("frontend/src/pages/Dashboard.tsx", "{greeting}, Captain {firstName}", "x")],
            repo,
        )
        assert all("cap is" in s["reason"] for s in planned["skip"])

    def test_lines_are_counted_as_review_effort_not_as_a_delta(self, repo):
        """Replacing three lines with five is eight lines to read, not two."""
        assert edits._lines_touched("a\nb\nc", "a\nb\nc\nd\ne") == 8


class TestWriteGuards:
    def test_a_file_that_changed_between_plan_and_write_is_refused(self, repo):
        edit = _edit(
            "frontend/src/pages/Dashboard.tsx", "{greeting}, Captain {firstName}", "x"
        )
        planned = edits.plan([edit], repo)
        (repo / "frontend/src/pages/Dashboard.tsx").write_text("something else entirely\n")
        with pytest.raises(RuntimeError, match="changed between plan and write"):
            edits.write(planned)

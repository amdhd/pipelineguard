"""
Applying the agent's structured edits.

PLAN.md Phase 2: "Have the agent return {file, old_string, new_string} objects
and apply them programmatically. Freeform unified diffs fail to apply
constantly -- the reference run is littered with findings marked 'could not
locate source; skipped' and 'agent produced no applicable diff'."

Structured edits do not make that failure impossible. They make it HONEST: an
edit whose `old_string` is not in the file, or is in it twice, is rejected here
with a reason a reviewer can read, instead of being silently half-applied by a
patch tool doing its best with fuzzy context.

WHY A BATCH IS ALL-OR-NOTHING
-----------------------------
Individually malformed edits are skipped and reported. But once the surviving
edits breach a batch threshold, NOTHING is written -- not the first N that fit.
A partially applied batch is worse than no batch at all: it compiles, or fails
to, for reasons nobody chose, and the PR summary would have to explain a state
that was never proposed by anyone. Refusing the whole batch leaves the tree
clean and the reason single.

NEW FILES ARE IMPOSSIBLE BY CONSTRUCTION
----------------------------------------
Every edit must match existing text, and an empty `old_string` is rejected
explicitly rather than treated as "insert at the start". So this applier can
only ever modify files that already exist inside the allow-list. That is a
property worth keeping: an agent that cannot create files cannot create one the
allow-list was never asked about.
"""

from pathlib import Path

import paths as path_rules

# Edit outcomes. Strings rather than an enum because they are rendered straight
# into the PR summary and archived in the run's JSON.
APPLIED = "applied"
SKIPPED = "skipped"


def _lines_touched(old: str, new: str) -> int:
    """
    How much of the file this edit disturbs.

    Deliberately the SUM of both sides rather than the difference: replacing
    three lines with five is eight lines a reviewer has to read, not two. The
    cap exists to bound review effort, so it counts the thing review actually
    costs, and it over-counts rather than under-counts.
    """
    return len(old.splitlines()) + len(new.splitlines())


def check_edit(edit: object, index: int) -> str | None:
    """Why this edit cannot be applied, or None. Shape only -- no file access."""
    where = f"edit[{index}]"
    if not isinstance(edit, dict):
        return f"{where} is not an object"
    for key in ("file", "old_string", "new_string"):
        if key not in edit:
            return f"{where} is missing '{key}'"
        if not isinstance(edit[key], str):
            return f"{where}.{key} must be a string"

    if not edit["old_string"]:
        # An empty old_string would make this a file-creation tool. It is not
        # one, and the allow-list is easier to reason about when the set of
        # writable files cannot grow.
        return f"{where}.old_string is empty; this applier cannot create content"
    if edit["old_string"] == edit["new_string"]:
        return f"{where} is a no-op; old_string and new_string are identical"

    return path_rules.reject_reason(edit["file"])


def _resolve(root: Path, relative: str) -> tuple[Path | None, str | None]:
    """
    Turn a checked relative path into a real one. Returns (path, reason); on
    refusal the path is None and the reason is reportable.

    This catches what the allow-list structurally cannot: a SYMLINK. The
    allow-list reasons about the string it was given, and `frontend/src/x.tsx`
    is a perfectly good string no matter what it points at.

    TWO different escapes, and the obvious check only catches one. A link out of
    the repo entirely fails `is_relative_to`. A link to `infra/main.tf` does
    NOT -- it lands inside the root and sails through, which is the more likely
    of the two and the more damaging. So the destination is re-checked against
    the same rules as the source: where a path LANDS has to be allowed, not just
    where it started.
    """
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        return None, f"resolves outside the repo: {relative}"

    landed = target.relative_to(root).as_posix()
    reason = path_rules.reject_reason(landed)
    if reason:
        return None, f"{relative} resolves to {landed}, which is refused: {reason}"
    return target, None


def plan(
    edits: list,
    root: Path,
    *,
    applied_files: frozenset = frozenset(),
    applied_lines: int = 0,
    read_only: frozenset = frozenset(),
) -> dict:
    """
    Decide what would happen, without writing anything.

    Returns {"apply": [...], "skip": [...], "lines": int, "files": int,
    "batch_error": str | None}. harness.py calls this, reports it, and only then
    calls `write`. Separating the decision from the write is what makes the
    all-or-nothing rule enforceable and the dry run free.

    THE CAPS ARE PER RUN, NOT PER FINDING. `applied_files` and `applied_lines`
    carry what earlier findings in the same run already spent, and the caller
    threads them through. Without that, a run with five findings could touch
    twenty-five files and four hundred lines while every individual batch passed
    a five-file cap -- and those numbers exist to bound the thing the exit
    criterion measures, "a human can review it in under ten minutes", which is a
    property of the PR and not of any one finding inside it.

    `read_only` is the manifest-declared contract context for this finding, and
    it is enforced HERE rather than only asked for in the prompt. The prompt
    says "reference only"; this is what makes that true. The failure it guards
    is specific and plausible: shown a type that disagrees with the wire, a
    model can silence the type-checker by editing the type -- leaving the crash
    exactly where it was and the declaration now lying in the other direction.
    See contracts.py.
    """
    to_apply: list[dict] = []
    to_skip: list[dict] = []

    for i, edit in enumerate(edits):
        reason = check_edit(edit, i)
        if reason:
            to_skip.append({"edit": edit, "status": SKIPPED, "reason": reason})
            continue

        relative = path_rules.normalise(edit["file"])
        if relative in read_only:
            to_skip.append(
                {
                    "edit": edit,
                    "status": SKIPPED,
                    "reason": f"{relative} is read-only API contract context, not editable "
                    "source; it was shown so the cause could be identified, not patched",
                }
            )
            continue
        target, reason = _resolve(root, relative)
        if target is None:
            to_skip.append({"edit": edit, "status": SKIPPED, "reason": reason})
            continue
        if not target.is_file():
            to_skip.append(
                {"edit": edit, "status": SKIPPED, "reason": f"file does not exist: {relative}"}
            )
            continue

        text = target.read_text(encoding="utf-8", errors="strict")
        occurrences = text.count(edit["old_string"])
        if occurrences == 0:
            # The honest version of the reference implementation's "could not
            # locate source". The agent was looking at the file; the text it
            # quoted is not in it.
            to_skip.append(
                {"edit": edit, "status": SKIPPED, "reason": f"old_string not found in {relative}"}
            )
            continue
        if occurrences > 1:
            to_skip.append(
                {
                    "edit": edit,
                    "status": SKIPPED,
                    "reason": f"old_string appears {occurrences} times in {relative}; "
                    "ambiguous, needs more surrounding context",
                }
            )
            continue

        to_apply.append(
            {
                "edit": edit,
                "status": APPLIED,
                "path": relative,
                "target": target,
                "lines": _lines_touched(edit["old_string"], edit["new_string"]),
            }
        )

    files = len({item["path"] for item in to_apply} | set(applied_files))
    lines = sum(item["lines"] for item in to_apply) + applied_lines

    batch_error = None
    if files > path_rules.MAX_FILES_TOUCHED:
        batch_error = (
            f"this run would touch {files} files, cap is "
            f"{path_rules.MAX_FILES_TOUCHED}"
        )
    elif lines > path_rules.MAX_LINES_CHANGED:
        batch_error = (
            f"this run would change {lines} lines, cap is "
            f"{path_rules.MAX_LINES_CHANGED}"
        )

    if batch_error:
        # All-or-nothing: everything that would have applied becomes a skip,
        # carrying the batch reason rather than an individual one.
        to_skip.extend(
            {"edit": item["edit"], "status": SKIPPED, "reason": batch_error} for item in to_apply
        )
        to_apply = []

    return {
        "apply": to_apply,
        "skip": to_skip,
        "files": files,
        "lines": lines,
        "batch_error": batch_error,
    }


def write(planned: dict) -> list[dict]:
    """
    Perform the writes a `plan` approved. Returns the applied items.

    Re-reads and re-counts immediately before writing. Nothing should have
    changed between plan and write -- the runner is single-threaded and nothing
    else touches the tree -- which is exactly why a mismatch here means an
    assumption is wrong, and a fix agent is the last program that should write
    through a surprise.
    """
    applied: list[dict] = []
    for item in planned["apply"]:
        target: Path = item["target"]
        edit = item["edit"]
        text = target.read_text(encoding="utf-8")
        if text.count(edit["old_string"]) != 1:
            raise RuntimeError(
                f"{item['path']} changed between plan and write; refusing to apply"
            )
        target.write_text(text.replace(edit["old_string"], edit["new_string"], 1), encoding="utf-8")
        applied.append(item)
    return applied

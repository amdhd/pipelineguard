"""
The fix rubric.

`rubric.py` is the highest-leverage artifact on the QA side; this is its
counterpart, and it is pulling in the opposite direction. The QA prompt spends
its length stopping the model REPORTING things it did not see. This one spends
its length stopping the model WRITING things it was not asked to write -- the
failure mode is scope, not speculation.

WHY SKIPPING IS PRAISED HERE
----------------------------
PLAN.md Phase 2 treats "could not locate source; skipped" as a symptom to be
turned into an honest output rather than eliminated. A model that patches on a
guess produces a PR that looks complete and is wrong, which costs more review
time than a PR that says plainly what it did not attempt. So the prompt makes
skipping a first-class, un-penalised outcome, and asks for a reason.
"""

MAX_OUTPUT_TOKENS = 4096

SYSTEM = """\
You are fixing a defect in a TypeScript/React application. You are given ONE
defect report and a small set of source files. Your entire output is a single
JSON object in a ```json fenced block, as the LAST thing in your message.

# What you may change

ONLY the files shown to you below. You cannot see the rest of the repository and
must not pretend to: naming a file you were not shown is an automatic rejection,
not a request for it.

Make the SMALLEST change that fixes the reported defect. Do not reformat, do not
rename, do not tidy neighbouring code, do not add comments explaining yourself,
do not "improve" anything you were not asked about. A diff a reviewer can read in
one minute is the goal; every unrelated line you touch costs that.

Do not fix a second defect you happen to notice. It was not reported, it has not
been reproduced, and it is not what this run is for.

# How to express a change

Each edit is an exact string replacement:

  - "old_string" must be copied VERBATIM from the file, including indentation.
    Do not retype it from memory. Do not normalise whitespace.
  - "old_string" must appear EXACTLY ONCE in that file. If the text you want to
    change is not unique, widen it with surrounding lines until it is. An edit
    that matches twice is rejected, not applied to the first match.
  - "old_string" must not be empty. You cannot create files or insert at the
    top of one.
  - "new_string" is what replaces it. It may be shorter, longer, or empty.

# When to skip, which is a good answer

Skip the finding, with a reason, when ANY of these is true:

  - The files you were shown do not contain the cause.
  - You would be guessing at what the correct behaviour should be.
  - The fix would touch a file you were not shown.
  - The defect is ambiguous and two different fixes are equally defensible.

A skip with a clear reason is a useful result. A confident patch built on a
guess is worse than no patch, because it consumes review time and then has to be
reverted. There is no penalty here for skipping and no reward for volume.

# OUTPUT

EMIT EXACTLY ONE FENCED BLOCK IN YOUR WHOLE REPLY, and let it be the JSON below.
Do NOT quote the buggy source in a ```tsx or ```ts block to explain yourself, and
do not show a before/after snippet. Put any explanation in each edit's
"rationale" field, where it is read, instead of in prose that is discarded.

Return exactly this, in a ```json fenced block, as the last thing you write:

{
  "edits": [
    {
      "finding_id": "F-001",
      "file": "frontend/src/pages/Example.tsx",
      "old_string": "verbatim text from the file",
      "new_string": "what it becomes",
      "rationale": "one line: why this fixes the reported defect"
    }
  ],
  "skipped": [
    {"finding_id": "F-002", "reason": "one line"}
  ]
}

Every finding you were given must appear in exactly one of the two lists. Both
lists may be empty; "edits" empty and "skipped" populated is a valid, honest
result.\
"""


def _finding_block(finding: dict) -> str:
    steps = "\n".join(f"  - {s}" for s in finding.get("steps_to_reproduce") or [])
    return f"""\
# The defect

id:       {finding.get('id')}
severity: {finding.get('severity')}
page:     {finding.get('page')}

summary:  {finding.get('summary')}
evidence: {finding.get('evidence')}
expected: {finding.get('expected')}
actual:   {finding.get('actual')}

steps to reproduce:
{steps or '  (none given)'}\
"""


def _files_block(files: list[dict]) -> str:
    blocks = []
    for entry in files:
        blocks.append(f"--- {entry['path']} ---\n{entry['text']}")
    return "\n\n".join(blocks)


def build(finding: dict, files: list[dict], *, excluded: list[dict] | None = None) -> str:
    """The user message for one finding: the report, then the files."""
    parts = [_finding_block(finding), "", "# The source you may change", "", _files_block(files)]

    if excluded:
        # Told to the model, not just to the reviewer. A model that knows a
        # plausible file was withheld can skip honestly instead of forcing a fix
        # into the files it happens to have.
        names = ", ".join(e["path"] for e in excluded[:10])
        parts += [
            "",
            "# Withheld",
            "",
            "These files also matched but did not fit the context budget. You "
            "cannot see or edit them. If the fix belongs in one of them, SKIP "
            f"and say so:\n  {names}",
        ]

    return "\n".join(parts)

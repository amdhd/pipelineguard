"""
Choosing which source files the fix agent gets to see.

NAMED sources.py, NOT select.py. `select` is a standard-library module, and it
is already in `sys.modules` before this package's path entry is added -- so
`import select` inside the tests resolved to the stdlib I/O-multiplexing module
and every assertion failed with `has no attribute`. The QA harness carries the
same scar under a different name: see the note about `handler.py` at the top of
agents/qa/harness/main.py. Shadowing a stdlib name fails late and reads as a
missing function.

PLAN.md Phase 2 calls this "the part the omission was hiding". vesselAI is a
six-module monorepo; uploading it is impractical and unnecessary, so the harness
selects a bounded file set in the runner where the repo is already checked out:

  1. Start from the finding's `suspected_source`.
  2. Fall back to a search seeded by the finding's `page` and `summary` when it
     is null.
  3. Cap the result, and record what was excluded.
  4. A finding whose source cannot be located within the cap is reported as
     SKIPPED WITH THE REASON, not guessed at.

THE FALLBACK IS THE NORMAL PATH, NOT THE EXCEPTION
--------------------------------------------------
`rubric.py` tells the QA agent to "prefer null for suspected_source over a
guess. A wrong guess sends the fix agent to the wrong file, which is worse than
no guess at all." It obeys: the committed fixture from run 33140664097 has
`suspected_source: null`. So step 2 is what actually runs, and building it as an
afterthought would mean building the whole feature as an afterthought.

THE ALLOW-LIST IS APPLIED HERE TOO, AND THAT IS THE POINT
---------------------------------------------------------
Selection only ever walks allow-listed paths, so the model is never shown a file
it would not have been permitted to patch. `edits.py` still checks -- the model
can name any path it likes regardless of what it was shown -- but a model that
never sees `infra/main.tf` is a model that cannot form the intention to edit it.
Refusing a bad proposal is good; not eliciting one is better.

DETERMINISTIC, LIKE THE CANDIDATE LAYER
---------------------------------------
Ranking is a pure function of the finding text and the tree. Ties break on path
order, never on filesystem walk order, so the same finding against the same
commit selects the same files every time. A selection step that varied run to
run would make every recall measurement above it meaningless.
"""

import re
from pathlib import Path

import paths as path_rules

# Files worth showing a code-fixing model. Deliberately code only: a finding
# about a blank figure is not fixed in a stylesheet or a fixture JSON, and every
# byte spent here is a byte of the context budget.
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Caps. MAX_BYTES is the one that matters -- it is the context budget in
# disguise, and at roughly four bytes per token it puts a full selection around
# 24k input tokens, which is the figure the cost estimate in PLAN.md Phase 2 is
# built on.
MAX_FILES = 8
MAX_BYTES = 96_000

# A single file larger than this is not read at all. A 500KB generated bundle
# would consume the entire byte cap and teach the model nothing.
MAX_FILE_BYTES = 40_000

# Term weights. A quoted literal lifted from the UI -- "Captain Captain" -- is
# the highest-signal query available: it is text the user SAW, so it almost
# always appears verbatim in the source that rendered it. A stray English word
# from the summary is the weakest.
WEIGHT_SUSPECTED = 100
WEIGHT_LITERAL = 5
WEIGHT_ROUTE = 3
WEIGHT_IDENTIFIER = 2
WEIGHT_WORD = 1

# Multiplier applied to a term that matches the file's PATH as well as, or
# instead of, its body. See score_file for the measurement behind it.
PATH_BONUS = 3

_STOPWORDS = frozenset(
    """
    a an and are as at be but by does for from has have in into is it its no not
    of on or that the their there these this to was were when which while with
    page shows show renders render display displayed value values field fields
    should would could when after before then than
    """.split()
)

# Quote delimiters must not be word-internal. Without the lookarounds, the
# apostrophe in "the user's stored display name is 'Captain Ahmad Fauzi'" opens
# a match at `user'` and closes it at the next apostrophe, yielding the term
# "s stored display name is " and DISCARDING both real literals. Measured
# against the committed fixture on the real repo: the old pattern extracted
# nothing but garbage, and the correct file was ranking first on weaker signals
# in spite of it.
_QUOTED = re.compile(r"(?<![A-Za-z0-9])[\"'`]([^\"'`\n]{3,60})[\"'`](?![A-Za-z0-9])")
_IDENTIFIER = re.compile(r"\b([a-z]+[A-Z][A-Za-z0-9]{2,}|[A-Z][a-z]{2,}[A-Z][A-Za-z0-9]*)\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


def search_terms(finding: dict) -> list[tuple[str, int]]:
    """
    Weighted query terms for one finding, most specific first.

    Deduplicated case-insensitively, keeping the highest weight seen for a term,
    so a word that also appears inside a quoted literal is scored as the literal.
    """
    text = " ".join(
        str(finding.get(key) or "")
        for key in ("summary", "evidence", "expected", "actual")
    )

    scored: dict[str, tuple[str, int]] = {}

    def add(term: str, weight: int) -> None:
        term = term.strip()
        if len(term) < 3:
            return
        key = term.lower()
        if key in _STOPWORDS:
            return
        if key not in scored or scored[key][1] < weight:
            scored[key] = (term, weight)

    for literal in _QUOTED.findall(text):
        add(literal, WEIGHT_LITERAL)

    # The route, when it is one. "/" is not a term -- it matches everything and
    # discriminates nothing -- and it is exactly the page the committed fixture
    # carries, which is why this is an explicit branch rather than an oversight.
    page = str(finding.get("page") or "").strip()
    for segment in page.split("/"):
        if segment:
            add(segment, WEIGHT_ROUTE)

    for identifier in _IDENTIFIER.findall(text):
        add(identifier, WEIGHT_IDENTIFIER)

    for word in _WORD.findall(str(finding.get("summary") or "")):
        add(word, WEIGHT_WORD)

    return sorted(scored.values(), key=lambda tw: (-tw[1], tw[0].lower()))


def candidate_files(root: Path) -> list[str]:
    """
    Every allow-listed source file in the tree, in a stable order.

    Sorted rather than walk-ordered: `Path.rglob` makes no ordering promise
    across platforms, and a selection that depends on it is a selection that
    varies between the runner and your laptop.
    """
    found: list[str] = []
    for prefix in path_rules.ALLOWED_PREFIXES:
        base = root / prefix
        if not base.is_dir():
            continue
        for entry in base.rglob("*"):
            if not entry.is_file() or entry.is_symlink():
                continue
            if entry.suffix not in SOURCE_SUFFIXES:
                continue
            relative = entry.relative_to(root).as_posix()
            if path_rules.reject_reason(relative) is None:
                found.append(relative)
    return sorted(found)


def score_file(text: str, terms: list[tuple[str, int]], path: str = "") -> int:
    """
    How well one file answers the finding.

    Counts DISTINCT terms matched, not occurrences. A file mentioning `greeting`
    forty times is not forty times more likely to be the right file, and scoring
    by occurrence reliably promotes the largest file in the tree.

    A term matching the PATH counts extra, and that is not a tie-breaker bolted
    on afterwards -- it is different evidence. Body text says the file mentions
    the subject; a filename says the file is ABOUT it.

    Measured, and the measurement corrected an assumption. On the committed
    fixture against the real repo, `backend/src/routes/auth.ts` outscored
    `frontend/src/pages/DashboardPage.tsx` on body text alone, because the
    finding quotes the seeded user name "Captain Ahmad Fauzi" -- which is DATA
    and appears verbatim in the seed -- while "Captain Captain" is RENDERED
    output assembled from a template and appears nowhere in source. Quoted UI
    text is a strong signal for the string, and can point at the wrong file for
    the template. The filename match is what separates them.

    PATH_BONUS is the smallest multiplier that puts the right file first on the
    one finding available to test it, chosen low deliberately. N=1 is not a
    measurement, and this repo has a documented habit of treating it as one --
    re-check the number when there are more findings to check it against.
    """
    lowered = text.lower()
    lowered_path = path.lower()
    score = sum(weight for term, weight in terms if term.lower() in lowered)
    score += sum(weight * PATH_BONUS for term, weight in terms if term.lower() in lowered_path)
    return score


def select(finding: dict, root: Path) -> dict:
    """
    The bounded file set for one finding.

    Returns {"files": [{path, text, score}], "excluded": [...], "terms": [...],
    "reason": str | None}. A non-None reason means no source was located and the
    finding must be reported as skipped -- never patched on a guess.
    """
    terms = search_terms(finding)
    seeded: list[str] = []

    suspected = finding.get("suspected_source")
    if isinstance(suspected, str) and suspected.strip():
        normalised = path_rules.normalise(suspected)
        if normalised and path_rules.reject_reason(normalised) is None:
            if (root / normalised).is_file():
                seeded.append(normalised)

    scored: list[tuple[int, str, str]] = []
    oversized: list[dict] = []
    for relative in candidate_files(root):
        target = root / relative
        try:
            size = target.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            # Report it, but only when its PATH suggests it was relevant.
            #
            # This used to be a bare `continue`, which made a size-skip
            # invisible: "the fix was in a 60KB file" and "no source matched"
            # produced identical output, and only one of those is something the
            # caps can be tuned for. Scoring the path alone costs no read, and
            # listing every large file in the tree regardless would bury the one
            # that mattered under noise.
            if score_file("", terms, relative) > 0:
                oversized.append(
                    {
                        "path": relative,
                        "score": score_file("", terms, relative),
                        "reason": f"exceeds MAX_FILE_BYTES ({size:,} > {MAX_FILE_BYTES:,})",
                    }
                )
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        score = WEIGHT_SUSPECTED if relative in seeded else score_file(text, terms, relative)
        if score > 0:
            scored.append((score, relative, text))

    # Highest score first; ties by path, so the result never depends on walk
    # order or on dict insertion.
    scored.sort(key=lambda item: (-item[0], item[1]))

    chosen: list[dict] = []
    excluded: list[dict] = list(oversized)
    total = 0
    for score, relative, text in scored:
        size = len(text.encode("utf-8"))
        if len(chosen) >= MAX_FILES:
            excluded.append({"path": relative, "score": score, "reason": f"file cap ({MAX_FILES})"})
            continue
        if total + size > MAX_BYTES:
            excluded.append(
                {"path": relative, "score": score, "reason": f"byte cap ({MAX_BYTES})"}
            )
            continue
        chosen.append({"path": relative, "text": text, "score": score})
        total += size

    reason = None
    if not chosen and oversized:
        # A distinct reason from "nothing matched". This one is actionable --
        # raise MAX_FILE_BYTES, or split the file -- and the other is not.
        names = ", ".join(e["path"] for e in oversized[:5])
        reason = (
            "the only matching source was too large to read "
            f"(MAX_FILE_BYTES={MAX_FILE_BYTES:,}): {names}"
        )
    elif not chosen:
        # The honest output PLAN.md asks for. Naming the terms that found
        # nothing is what makes this actionable: it distinguishes "the rubric
        # wrote a vague summary" from "the file is outside the allow-list".
        top = ", ".join(term for term, _ in terms[:6]) or "none"
        reason = f"no allow-listed source matched this finding (terms tried: {top})"

    return {"files": chosen, "excluded": excluded, "terms": terms, "reason": reason, "bytes": total}

"""
Repo-declared API contract context for the fix model.

WHY THIS EXISTS, measured rather than assumed
---------------------------------------------
vesselAI PR #130 was an agent fix that mis-diagnosed its own finding. The report
said "/voyage crashes with a TypeError on 'toFixed'"; the agent guarded
`ciiImpact`. The actual cause was `/voyage/history` sending `actual_fuel` where
the client reads `actualFuel`, so `voyage.actualFuel` was `undefined` and
`formatFuel(...)` threw.

The selection for that run was replayed on 2026-09-05 (vesselAI
`d4-demo/BASELINE.md`). It is worth reading carefully, because it refutes the
obvious explanation:

    F-001's eight slots went to
      1. frontend/src/modules/voyage/VoyageHistory.tsx   <- the crashing component
      2. backend/src/routes/voyage.ts                    <- the other side of the boundary
      3-7. VoyagePage, RouteOptimizer, SpeedOptimizer, WeatherPanel, AgentPlanner
      8. backend/src/services/voyageAgent.eval.test.ts

The model SAW both sides of the boundary, with the wrong key visible in the
route, and still fixed the wrong thing. So the problem was never proximity, and
"show it the file that has the bug" was already solved.

What it never saw -- all three withheld at `file cap (8)`, all scoring 6, just
under the cut -- was the material that makes the mismatch LEGIBLE:

    frontend/src/lib/types.ts   VoyageHistoryRecord.actualFuel: number (non-null)
    frontend/src/lib/api.ts     the getHistory call asserting that type
    frontend/src/lib/utils.ts   formatFuel(mt: number), the unguarded .toFixed()

Without those, `actual_fuel` in a response literal is an unfamiliar key. With
them, it is a type the code asserts and the wire does not honour.

WHY A MANIFEST RATHER THAN BETTER RANKING
-----------------------------------------
The tempting fix is to raise MAX_FILES, or to teach score_file about type
declarations. Neither works on the measured data. Those three files score 6; the
irrelevant siblings that beat them score 7-12, and `voyageAgent.test.ts` scores
12. Raising the cap admits the siblings' successors, not the contract. Ranking
by text similarity cannot distinguish "mentions the same words" from "defines
the shape of the data", because that is a fact about the ARCHITECTURE, not about
the text.

So the repo declares it. `.qa-contracts.json` at the target's root says: for
this feature, these files define the contract. It is checked in next to the code
it describes, reviewed like code, and versioned with it.

WHY THE CONTEXT IS READ-ONLY BY DEFAULT
---------------------------------------
A model shown a type declaration that disagrees with the wire has two ways to
make them agree, and one of them is catastrophic: change `actualFuel: number` to
`actual_fuel: number` and the type-checker goes quiet while the crash stays. The
default is therefore reference-only, enforced in `edits.py` rather than merely
requested in the prompt.

THE TRAP THAT SHAPED THE `editable` FLAG
----------------------------------------
An earlier sketch of this made every manifest file read-only. That would have
been worse than doing nothing: `backend/src/routes/voyage.ts` is both the
boundary AND the file the correct fix belongs in. Declaring it as context would
have made the one defect this feature exists to fix unfixable.

Hence `editable: true`, which does not add a read-only file at all -- it forces
the path into the EDITABLE selection at suspected-source weight, guaranteeing
the model can both see it and patch it. Two different jobs, one manifest:

    editable: false (default)  "understand this, do not touch it"
    editable: true             "you will need to change this; here it is"

A path is never in both sets. `sources.select` resolves that precedence
explicitly.

FAILING LOUD ON A STALE MANIFEST
--------------------------------
Line numbers rot. A slice that has drifted onto the wrong lines is worse than no
slice, because it is presented to the model as authoritative fact about the
contract -- the exact failure mode (a confident lie about the data's shape) that
this module exists to prevent.

So slices are anchored to a literal string rather than to line numbers, and an
anchor that no longer matches does NOT silently fall back to a guessed offset.
It includes the whole file and records a `manifest_stale` warning that surfaces
in the run summary and the result JSON.

An AMBIGUOUS anchor is the same bug wearing a different costume, and it was
found while authoring vesselAI's own manifest rather than reasoned about in
advance: `getDocuments: async` appears in both knowledgeApi and sireApi in one
api.ts, so a sire feature anchored on it would have been handed the KNOWLEDGE
slice and told it was the sire contract. Taking the first match is a guess
presented as a fact. Both failures resolve the same way -- show the whole file,
warn loudly -- because too much true context beats a little false context.
"""

import json
from pathlib import Path

import paths as path_rules

MANIFEST_NAME = ".qa-contracts.json"

# Bumped only for a breaking schema change. An unknown version is ignored with a
# warning rather than parsed hopefully: a harness pinned to an older SHA reading
# a newer manifest should say so, not improvise.
SUPPORTED_VERSION = 1

# Lines taken from an anchor when the entry does not say. Wide enough for an
# interface or a small function, narrow enough that six of them stay well inside
# the context budget.
DEFAULT_SPAN = 24

# A slice is a slice. An entry asking for more than this wants the whole file and
# should say so by omitting the anchor.
MAX_SPAN = 200


class ManifestError(Exception):
    """The manifest exists but could not be read as one."""


def load(root: Path) -> tuple[dict | None, list[str]]:
    """
    Read `.qa-contracts.json` from the target repo root.

    Returns (manifest, warnings). A missing file is not an error and not a
    warning -- most repos will not have one, and the whole feature is opt-in.

    A malformed one IS a warning rather than an exception. The manifest is an
    optimisation; a typo in it must not take down a run that would otherwise
    produce a correct fix from ordinary selection.
    """
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return None, []

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, [f"{MANIFEST_NAME} could not be parsed and was ignored: {e}"]

    if not isinstance(raw, dict):
        return None, [f"{MANIFEST_NAME} is not a JSON object; ignored"]

    version = raw.get("version")
    if version != SUPPORTED_VERSION:
        return None, [
            f"{MANIFEST_NAME} declares version {version!r}, but this harness "
            f"understands version {SUPPORTED_VERSION}; ignored"
        ]

    features = raw.get("features")
    if not isinstance(features, list):
        return None, [f"{MANIFEST_NAME} has no 'features' list; ignored"]

    return raw, []


def _match_score(finding: dict, feature: dict) -> int:
    """
    How strongly one feature claims this finding. 0 means it does not.

    Two independent signals, because the findings that need this most are the
    ones with the weakest routing information:

      page_prefix  the finding's page starts with it. Precise when present.
      terms        any term appears in the finding's summary. This is the
                   fallback that matters -- a dashboard finding carries
                   `page: "/"`, which prefixes everything and discriminates
                   nothing, and sources.py already treats "/" as a non-term for
                   the same reason.

    Prefix beats terms, and a longer prefix beats a shorter one, so a repo can
    declare a broad feature and a narrow one without them fighting.
    """
    match = feature.get("match")
    if not isinstance(match, dict):
        return 0

    score = 0

    prefix = match.get("page_prefix")
    page = str(finding.get("page") or "").strip()
    if isinstance(prefix, str) and prefix and page.startswith(prefix):
        # "/" as a prefix would match every finding; require it to be a real
        # route segment, exactly as search_terms refuses "/" as a term.
        if prefix.strip("/"):
            score += 100 + len(prefix)

    terms = match.get("terms")
    if isinstance(terms, list):
        haystack = " ".join(
            str(finding.get(key) or "")
            for key in ("summary", "evidence", "expected", "actual")
        ).lower()
        for term in terms:
            if isinstance(term, str) and term.strip() and term.lower() in haystack:
                score += 1

    return score


def match_feature(finding: dict, manifest: dict | None) -> dict | None:
    """The best-matching feature for one finding, or None."""
    if not manifest:
        return None

    best: tuple[int, int, dict] | None = None
    for index, feature in enumerate(manifest.get("features") or []):
        if not isinstance(feature, dict):
            continue
        score = _match_score(finding, feature)
        if score <= 0:
            continue
        # Ties break on declaration order, never on dict iteration, so the same
        # finding against the same manifest always picks the same feature --
        # sources.py's determinism rule applies here too.
        if best is None or score > best[0]:
            best = (score, index, feature)

    return best[2] if best else None


def _slice(text: str, entry: dict, path: str) -> tuple[str, str | None]:
    """
    Cut the declared region out of a file's text.

    Returns (text, warning). The warning is the whole point of this function's
    shape: every failure path returns the FULL file rather than a guess, so a
    drifted manifest degrades to "too much context" instead of "confidently
    wrong context".
    """
    anchor = entry.get("anchor")
    if isinstance(anchor, str) and anchor.strip():
        lines = text.splitlines()
        hits = [i for i, line in enumerate(lines) if anchor in line]

        if not hits:
            return text, (
                f"manifest_stale: anchor {anchor!r} not found in {path}; "
                "showing the whole file instead of a guessed line range"
            )

        if len(hits) > 1:
            # AMBIGUITY IS THE SAME BUG AS STALENESS, and it was found the hard
            # way while authoring vesselAI's manifest: `getDocuments: async`
            # appears in both knowledgeApi and sireApi in the same api.ts, so
            # the sire feature would have been handed the knowledge slice and
            # told it was the sire contract.
            #
            # Taking the first match is a guess wearing the costume of a fact,
            # which is exactly what this module exists to prevent. Refuse to
            # slice: the whole file is merely too much context, and too much
            # true context beats a little false context every time.
            return text, (
                f"manifest_ambiguous: anchor {anchor!r} matches {len(hits)} lines "
                f"in {path} (first at line {hits[0] + 1}); showing the whole file. "
                "Make the anchor unique."
            )

        span = entry.get("span", DEFAULT_SPAN)
        if not isinstance(span, int) or not 1 <= span <= MAX_SPAN:
            span = DEFAULT_SPAN
        return "\n".join(lines[hits[0] : hits[0] + span]), None

    span_lines = entry.get("lines")
    if isinstance(span_lines, list) and len(span_lines) == 2:
        start, end = span_lines
        if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
            lines = text.splitlines()
            if end > len(lines):
                return text, (
                    f"manifest_stale: {path} has {len(lines)} lines but the "
                    f"manifest asks for {start}-{end}; showing the whole file"
                )
            return "\n".join(lines[start - 1 : end]), None

    return text, None


def resolve(feature: dict | None, root: Path) -> dict:
    """
    Turn a matched feature into concrete context.

    Returns {"readonly": [{path, text}], "editable": [paths], "warnings": [...]}.

    `editable` is a list of PATHS, not of file bodies: sources.select already
    knows how to read and score a file, and duplicating that here would mean two
    places deciding what a selected file looks like.
    """
    out: dict = {"readonly": [], "editable": [], "warnings": []}
    if not feature:
        return out

    entries = feature.get("context")
    if not isinstance(entries, list):
        return out

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue

        relative = path_rules.normalise(raw_path)
        if not relative:
            out["warnings"].append(f"manifest path {raw_path!r} is not a usable path; skipped")
            continue

        # The SAME allow-list the editable selection walks. A manifest is
        # repo-declared, which makes it trusted-ish, not trusted: it must not
        # become a way to put infra/main.tf in front of a code-writing model.
        reason = path_rules.reject_reason(relative)
        if reason is not None:
            out["warnings"].append(f"manifest path {relative} is not allow-listed ({reason}); skipped")
            continue

        if entry.get("editable") is True:
            if relative not in out["editable"]:
                out["editable"].append(relative)
            continue

        # Read-only entries are deduplicated on the SLICED identity, so one file
        # may legitimately contribute two regions (a request type and a response
        # type in the same types.ts) without being read twice.
        target = root / relative
        if not target.is_file():
            out["warnings"].append(f"manifest_stale: {relative} does not exist; skipped")
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            out["warnings"].append(f"manifest path {relative} could not be read ({e}); skipped")
            continue

        sliced, warning = _slice(text, entry, relative)
        if warning:
            out["warnings"].append(warning)

        key = f"{relative}::{entry.get('anchor') or entry.get('lines') or ''}"
        if key in seen:
            continue
        seen.add(key)
        out["readonly"].append({"path": relative, "text": sliced})

    return out


def for_finding(finding: dict, root: Path) -> dict:
    """
    The whole path, for one finding: load, match, resolve.

    Returns the same shape as `resolve`, plus "feature" (the matched id, or
    None) so the run's result JSON can record which declaration was used --
    "why did it show me these files" must be answerable from the artefact
    without re-reading the manifest.
    """
    manifest, warnings = load(root)
    feature = match_feature(finding, manifest)
    resolved = resolve(feature, root)
    resolved["warnings"] = warnings + resolved["warnings"]
    resolved["feature"] = feature.get("id") if feature else None
    return resolved

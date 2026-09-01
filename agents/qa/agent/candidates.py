"""
Deterministic candidate findings.

WHY THESE EXIST, AND WHY THEY ARE DETERMINISTIC. The discriminator run that
settled the S-2 question proved the bottleneck was not the model rung: sonnet
was handed a pristine signal -- a single `repeated_slots` group, kind
`svg-adjacent`, count 13, the exact shape the rubric calls "evidence in itself"
-- and still reported nothing. Two different model rungs, one clean harvest,
zero findings. The signal was present in every page read and neither model
connected it to a finding.

That failure mode is structural: a model-driven agent only reports what it
NOTICES, and a quiet blank ring has nothing loud to be noticed by. So the
mechanical classes move OUT of the model's attention and INTO the runtime.
`detect()` below turns a page state into a small list of CANDIDATES -- signals
that are cheap and unambiguous to compute -- and the agent is required to assess
every one of them (confirm as a finding, or refute with a reason). This removes
the "the model must notice" step without reintroducing the false-positive
guard's hole: the model still decides, it just cannot silently walk past.

The classes are deliberately the MECHANICAL ones only. One half of S-3 -- the
wrong colour that renders a plausible-looking number -- is not a deterministic
shape and stays model-driven; but the other half IS mechanical: a status VALUE
whose stored spelling leaks into the text, detectable as an all-caps word on a
page that renders the same class of value lowercase. That is
`status_case_leak`. Stdlib only, so the deployment zip stays pure Python.
"""

import re

# Console entries prefixed like this are decoration noise, not defects. The
# frontend's manifest icon reliably warns on every page load; a health-check
# probe can log warnings for endpoints that are fine. Only the error shapes
# (uncaught exceptions, `error:` entries) signal breakage. cdp._on_event writes
# exactly three prefixes -- "uncaught: ", "error: ", "warning: " (the CDP
# `type`/`level` value becomes the prefix); keep this in sync with it.
_WARNING_PREFIX = "warning:"

# Status vocabulary for the raw-enum-leak detector (`status_case_leak`). These
# are the VALUE class a page would normalise to lowercase; an all-caps
# textContent instance of one, on a page that renders the same class lowercase
# elsewhere, is an enum leaking raw into the text. Deliberately value-shaped:
# colours (green/amber/red), labels (status/date), and route words are not
# statuses and never belong here. Underscored forms (in_transit) are excluded
# from the lowercase-twin check because innerText renders them with spaces.
STATUS_WORDS = frozenset({
    "open", "closed", "pending", "active", "inactive", "completed",
    "scheduled", "draft", "approved", "rejected", "cancelled", "canceled",
    "valid", "expired", "ready", "resolved", "acknowledged", "shipped",
    "delivered", "in_progress", "unavailable", "offline", "online",
    "succeeded", "failed", "running", "queued", "blocked", "anchored",
    "healthy", "degraded", "critical", "normal", "congested", "missing",
})

# Underscored statuses render with a space in innerText ("in transit"), so they
# can never appear as a single lowercase token; they stay part of the all-caps
# vocabulary but not of the twin check.
_LOWERCASE_TWIN_WORDS = tuple(sorted(w for w in STATUS_WORDS if "_" not in w))

# Samples are capped so one candidate never carries a wall of context text into
# the context window. The count is the evidence; the samples are for the model
# to recognise the shape.
MAX_SAMPLES = 3


def _status_case_leak(state: dict) -> list[dict]:
    """
    An enum value leaking raw into the text: a status word rendered ALL-CAPS on
    a page that renders the same class of value lowercase elsewhere. The
    harvest collects all-caps words from the SOURCE case (CSS text-transform
    never reaches a text node), so this can never fire on a CSS-uppercased
    label. It fires only on an INCONSISTENCY: if every status on the page is
    stored uppercase, uppercase is the convention, not a leak -- hence the
    lowercase-twin requirement. Acronyms (IMO, CII, MT) are filtered by the
    vocabulary; `state["text"]` is innerText, so a lowercase twin there is a
    status the page really renders lowercase.
    """
    cap_words = state.get("case_words") or []
    if not cap_words:
        return []
    leaked = [w for w in cap_words if (w.get("word") or "").lower() in STATUS_WORDS]
    if not leaked:
        return []
    # Tokenise the RAW innerText (no .lower()): innerText reflects text-transform,
    # and CSS only ever uppercases, so a lowercase token there is a status the
    # page genuinely renders lowercase.
    tokens = set(re.findall(r"[a-z]+", state.get("text") or ""))
    lower_twins = {w for w in _LOWERCASE_TWIN_WORDS if w in tokens}
    if not lower_twins:
        return []
    out = []
    for w in leaked:
        count = w.get("count", 1)
        out.append({
            "type": "status_case_leak",
            "count": count,
            "samples": (w.get("sample") or [])[:MAX_SAMPLES],
            "evidence": (
                f"status value {w['word']!r} renders raw ALL-CAPS "
                f"({count} occurrence(s)) while the same page renders status "
                f"values lowercase ({', '.join(sorted(lower_twins))}) -- the "
                "stored spelling is leaking into the text unnormalised"
            ),
        })
    return out


def detect(state: dict) -> list[dict]:
    """
    Mechanical candidates from a page state dict. Tolerates missing keys --
    `_state()` may be partially assembled on a broken page, and a detector that
    crashes the read it is meant to enrich is worse than no detector.

    Each entry is {type, count, samples, evidence}. Ids are NOT assigned here:
    the session owns the id space so it can dedup across reads.
    """
    out: list[dict] = []

    for group in state.get("repeated_slots") or []:
        if group.get("kind") != "svg-adjacent":
            continue
        count = group.get("count", 0)
        if count < 2:
            continue
        samples = (group.get("sample") or [])[:MAX_SAMPLES]
        out.append({
            "type": "repeated_svg_empty",
            "count": count,
            "samples": samples,
            "evidence": (
                f"repeated_slots svg-adjacent group with count={count} "
                f"samples={samples} -- the same blank figure slot beside a "
                "chart/ring on every list item, which is what a field dropped "
                "from a list response looks like"
            ),
        })

    errors = [
        e for e in (state.get("console_errors") or [])
        if not str(e).lower().startswith(_WARNING_PREFIX)
    ]
    if errors:
        out.append({
            "type": "console_error",
            "count": len(errors),
            "samples": [str(e)[:200] for e in errors[:MAX_SAMPLES]],
            "evidence": f"{len(errors)} genuine console error(s) captured by CDP",
        })

    failed = state.get("failed_requests") or []
    if failed:
        out.append({
            "type": "failed_request",
            "count": len(failed),
            "samples": [str(f)[:200] for f in failed[:MAX_SAMPLES]],
            "evidence": f"{len(failed)} failed network request(s) captured by CDP",
        })

    out.extend(_status_case_leak(state))
    return out

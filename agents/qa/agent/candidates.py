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

The classes are deliberately the MECHANICAL ones only. S-3 (a wrong colour or
status that renders a plausible-looking number) has no deterministic shape and
stays model-driven -- that is the sonnet rung's job. Stdlib only, so the
deployment zip stays pure Python.
"""

# Console entries prefixed like this are decoration noise, not defects. The
# frontend's manifest icon reliably warns on every page load; a health-check
# probe can log warnings for endpoints that are fine. Only the error shapes
# (uncaught exceptions, `error:` entries) signal breakage. cdp._on_event writes
# exactly three prefixes -- "uncaught: ", "error: ", "warning: " (the CDP
# `type`/`level` value becomes the prefix); keep this in sync with it.
_WARNING_PREFIX = "warning:"

# Samples are capped so one candidate never carries a wall of context text into
# the context window. The count is the evidence; the samples are for the model
# to recognise the shape.
MAX_SAMPLES = 3


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

    return out

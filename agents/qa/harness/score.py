"""
Scoring a QA run against a labelled corpus.

WHY THIS EXISTS
---------------
Phase 1's exit criteria are quantitative -- "the false-positive rate is low
enough that the report is worth reading" -- and nothing in this repo could
compute either rate. That left the agent's output PLAUSIBLE rather than
DEMONSTRATED, which matters more than it sounds: Phase 2 patches whatever
Phase 1 reports, and Phase 3's convergence check compares finding sets across
rounds. Both inherit the detector's error rates without ever measuring them.

The two rates answer different questions and need different inputs:

  * FALSE POSITIVES cost human review time, which is the expensive resource.
    Measured from REAL runs, and only a human can label them -- deciding whether
    a finding is a defect or by-design is the judgement the agent is imitating,
    so a scorer that decided it automatically would be marking its own homework.

  * FALSE NEGATIVES are what no amount of watching real PRs will tell you: a run
    that finds nothing looks identical whether the app is clean or the agent is
    blind. Measured against SEEDED bugs, where the answer is known in advance
    because you put them there.

THE HONESTY RULE HERE
---------------------
An unlabelled finding is counted as unlabelled, never as correct. A rate
computed over a partially-labelled run would read as a measurement while being
an artefact of how much labelling somebody had got through -- the same class of
quietly-wrong number that `unpriced` exists to avoid in pricing.py. So the rate
is None until there is something to divide, and the count of findings still
awaiting a human is reported beside every figure.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import schema  # noqa: E402

# What a human may write against a finding.
TRUE_POSITIVE = "true-positive"
FALSE_POSITIVE = "false-positive"
BY_DESIGN = "by-design"

# by-design counts as a false positive for the RATE -- the reviewer's time went
# the same way either way -- but stays a separate label, because the two have
# different fixes. A false positive is a rubric problem; a by-design report is a
# rubric that has fallen out of sync with the target's own documentation.
NOT_A_DEFECT = (FALSE_POSITIVE, BY_DESIGN)
LABELS = (TRUE_POSITIVE, FALSE_POSITIVE, BY_DESIGN)


def fingerprints(findings: dict) -> dict:
    """Map each finding to its stable cross-run identity."""
    return {schema.finding_fingerprint(f): f for f in findings.get("findings", [])}


def _detects(finding: dict, seed: dict) -> bool:
    """
    Best-effort match between a reported finding and a seeded bug.

    Page must agree, and at least one of the seed's keywords must appear in the
    finding's own words. Deliberately narrow: a loose match inflates the
    detection rate, which is the direction that flatters the agent, and this
    number exists to be trusted rather than to be good.
    """
    if finding.get("page") != seed.get("page"):
        return False
    haystack = " ".join(
        str(finding.get(k, "")) for k in ("summary", "evidence", "actual")
    ).lower()
    return any(kw.lower() in haystack for kw in seed.get("keywords", []))


def score(findings: dict, corpus: dict) -> dict:
    """Score one run. `corpus` carries the seeded bugs and the human labels."""
    reported = fingerprints(findings)
    labels = corpus.get("labels", {})
    seeded = corpus.get("seeded", [])

    labelled = {fp: labels[fp] for fp in reported if fp in labels}
    unlabelled = [fp for fp in reported if fp not in labels]
    not_defects = [fp for fp, v in labelled.items() if v in NOT_A_DEFECT]

    # Over LABELLED findings only, and None when nothing is labelled yet.
    fp_rate = (len(not_defects) / len(labelled)) if labelled else None

    detected, missed = [], []
    for seed in seeded:
        hit = next(
            (fp for fp, f in reported.items() if _detects(f, seed)),
            # An explicit label wins over the keyword match: a human who says
            # "F-003 is seed S-2" is more reliable than any string comparison.
            next((fp for fp, v in labels.items() if v == seed["id"] and fp in reported), None),
        )
        (detected if hit else missed).append(seed["id"])

    fn_rate = (len(missed) / len(seeded)) if seeded else None

    return {
        "total_findings": len(reported),
        "labelled": len(labelled),
        "unlabelled": len(unlabelled),
        "needs_review": unlabelled,
        "true_positives": sum(1 for v in labelled.values() if v == TRUE_POSITIVE),
        "not_defects": len(not_defects),
        "false_positive_rate": fp_rate,
        "seeded_total": len(seeded),
        "seeded_detected": detected,
        "seeded_missed": missed,
        "false_negative_rate": fn_rate,
    }


def compare(a: dict, b: dict) -> dict:
    """
    Cross-rung comparison, for the benchmark Phase 1 asks for.

    The reference implementation ran one round on a weaker rung and got findings
    stronger models never reproduced; they were agent noise. A finding only one
    rung reports is not automatically wrong -- but it is the set worth reading
    first, and this is what puts that set in front of you.
    """
    fa, fb = fingerprints(a), fingerprints(b)
    both = set(fa) & set(fb)
    union = set(fa) | set(fb)
    return {
        "a_only": sorted(set(fa) - set(fb)),
        "b_only": sorted(set(fb) - set(fa)),
        "both": sorted(both),
        "agreement": (len(both) / len(union)) if union else None,
    }


def _pct(value) -> str:
    return "not measured" if value is None else f"{value:.0%}"


def render(result: dict) -> str:
    lines = [
        "# QA run score",
        "",
        f"- Findings reported: **{result['total_findings']}**",
        f"- Labelled by a human: **{result['labelled']}**"
        f" ({result['unlabelled']} still awaiting review)",
        f"- False-positive rate: **{_pct(result['false_positive_rate'])}**"
        f" ({result['not_defects']} of {result['labelled']} labelled were not defects)",
        f"- False-negative rate: **{_pct(result['false_negative_rate'])}**"
        f" ({len(result['seeded_missed'])} of {result['seeded_total']} seeded bugs missed)",
    ]
    if result["seeded_missed"]:
        lines += ["", f"**Missed seeds:** {', '.join(result['seeded_missed'])}"]
    if result["unlabelled"]:
        lines += [
            "",
            "The false-positive rate above covers only the labelled findings. "
            "Label the rest before quoting it:",
            "",
        ]
        lines += [f"  - `{fp}`" for fp in result["needs_review"]]
    return "\n".join(lines)


def render_comparison(result: dict, a_name: str, b_name: str) -> str:
    return "\n".join(
        [
            "# Rung comparison",
            "",
            f"- Agreement: **{_pct(result['agreement'])}**",
            f"- Only `{a_name}`: {len(result['a_only'])}",
            f"- Only `{b_name}`: {len(result['b_only'])}",
            f"- Both: {len(result['both'])}",
            "",
            "Findings reported by one rung and not the other are where the noise "
            "usually is. Read those first.",
        ]
    )


def main(argv=None) -> int:  # pragma: no cover -- thin CLI wrapper
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--findings", help="findings JSON from a run")
    p.add_argument("--corpus", help="seeded bugs + human labels")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="two findings files")
    args = p.parse_args(argv)

    if args.compare:
        a, b = (json.loads(Path(f).read_text()) for f in args.compare)
        print(render_comparison(compare(a, b), *args.compare))
        return 0
    if not args.findings:
        p.error("--findings is required unless --compare is given")
    findings = json.loads(Path(args.findings).read_text())
    corpus = json.loads(Path(args.corpus).read_text()) if args.corpus else {}
    print(render(score(findings, corpus)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""
Rendering the convergence loop's report.

NAMED ledger.py because `report` and `summary` are both taken by the QA and fix
harnesses, and because the reconciliation ledger is the thing this module exists
to produce. The other two render one run; this one renders a HISTORY, and the
difference is the whole value: a reviewer looking at round 3 in isolation cannot
tell a defect the agent fixed from a defect it never reported.

Three sections, in the order a reader needs them:

  1. What happened per round -- did the set shrink, and what did that cost.
  2. The per-finding ledger PLAN.md Phase 3 asks for, which is the only place
     "fixed in round 2" is visible at all.
  3. The cumulative cost, because this is the phase that multiplies spend by the
     round count and a total nobody prints is a total nobody checks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa" / "harness"))

import convergence as conv  # noqa: E402
import pricing  # noqa: E402

MARKER = "<!-- pipelineguard-qa-converge -->"

ICON = {
    conv.PASS: "✅",
    conv.CONTINUE: "🔁",
    conv.STALL: "⛔",
    conv.REGRESSED: "🔴",
    conv.INCONCLUSIVE: "⚠️",
    conv.MAX_ROUNDS: "🛑",
    conv.TOKEN_BUDGET: "🛑",
    conv.WALL_CLOCK: "🛑",
    conv.ROUND_TIMEOUT: "🛑",
}

# What the reader should DO. A stop state that explains itself and then leaves
# the reader without a next action costs the same as one that does not explain
# itself at all -- the same argument report.py's _ERROR_HINTS makes.
NEXT = {
    conv.PASS: (
        "Nothing further. The blocking findings from the first round are gone and "
        "the final round observed none. The agent's patches are on this branch and "
        "still need a human review -- a converged loop is evidence the defects are "
        "gone, not that the patches are good."
    ),
    conv.CONTINUE: (
        "Another round is starting. This comment will be replaced when the loop ends."
    ),
    conv.STALL: (
        "**Hand to a human.** The agent has stopped making progress on the findings "
        "below, so more rounds would re-run the same agent against the same defects "
        "at the same price. Read the outstanding rows in the ledger: a finding the "
        "fix agent skipped will say why, and a finding it patched without effect is "
        "usually one whose cause is not in the file the patch reached."
    ),
    conv.REGRESSED: (
        "**Hand to a human, and treat the branch with suspicion.** A blocking finding "
        "appeared that the previous round did not report. The agent's own patch is the "
        "most likely cause -- the compile-and-test gate passed, which means the "
        "regression is in behaviour the suite does not cover. Review the `introduced` "
        "rows in the ledger against the diff before merging anything."
    ),
    conv.INCONCLUSIVE: (
        "**Re-run, do not read this as a result.** The final round failed or was "
        "truncated, so its finding set is smaller for reasons that have nothing to do "
        "with the application. The cause is in that round's QA job -- its comment and, "
        "if the runtime was reached, its CloudWatch log group."
    ),
    conv.MAX_ROUNDS: (
        "**Hand to a human.** The loop was still making progress when it hit its hard "
        "round cap, so the remaining findings below are not necessarily unfixable -- "
        "they are unfinished. Re-running the loop on the agent's branch continues from "
        "where this one stopped."
    ),
    conv.TOKEN_BUDGET: (
        "**Hand to a human.** The loop stopped on its cumulative token budget rather "
        "than on the findings. Raise the budget only after reading the per-round costs "
        "below: a round that cost far more than its neighbours usually means the prompt "
        "cache stopped matching, not that the budget was too small."
    ),
    conv.WALL_CLOCK: (
        "**Hand to a human.** The loop stopped on its cumulative wall-clock cap. Check "
        "the per-round seconds below -- a single slow round is a different problem from "
        "three ordinary ones."
    ),
    conv.ROUND_TIMEOUT: (
        "**Hand to a human.** A round overran its own wall-clock cap, so the loop will "
        "not start another. The findings below are real; the timing is the thing to "
        "look at first."
    ),
}


def _table(headers: list, rows: list) -> str:
    if not rows:
        return "_none_"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |")
    return "\n".join(out)


def _rounds_table(rounds: list) -> str:
    rows = []
    for i, r in enumerate(rounds):
        before = set(rounds[i - 1]["blocking"]) if i else set()
        after = set(r["blocking"])
        # Round 1 has nothing to compare against, and "0 resolved" would read as
        # a stall rather than as a baseline.
        resolved = "—" if not i else len(before - after)
        appeared = "—" if not i else len(after - before)
        note = ""
        if r["error"]:
            note = f" _(failed: {r['error']})_"
        elif r["incomplete"]:
            note = f" _(partial: {r.get('stop_reason') or 'budget exhausted'})_"
        rows.append(
            [
                f"{r['round']}{note}",
                len(after),
                resolved,
                appeared,
                len(r["patched"]),
                f"{r['meters']['session_seconds']}s",
                pricing.fmt_usd(r["meters"]["qa_usd"]),
                pricing.fmt_usd(r["meters"]["fix_usd"]) if r["meters"]["fix_calls"] else "—",
            ]
        )
    return _table(
        ["Round", "Blocking", "Resolved", "New", "Patched", "Session", "QA", "Fix"],
        rows,
    )


def _ledger_table(rows: list) -> str:
    return _table(
        ["Severity", "Page", "Finding", "Rounds", "Patched in", "Outcome", "Human label"],
        [
            [
                r["severity"],
                f"`{r['page']}`",
                r["summary"],
                ", ".join(str(n) for n in r["rounds_seen"]),
                ", ".join(str(n) for n in r["patched_in"]) or "—",
                r["outcome"],
                # score.py's rule, restated in the place a reader is most likely
                # to want it filled in for them: an unlabelled finding is
                # unlabelled, never "probably fine".
                r["label"] or "_needs a human_",
            ]
            for r in rows
        ],
    )


def _cost_table(rounds: list) -> str:
    agg = conv.totals(rounds)
    tokens = f"{agg['tokens']:,}"
    if not agg["tokens_known"]:
        tokens += " (a floor — see below)"
    lines = [
        _table(
            ["Meter", "Units", "Estimated"],
            [
                ["Rounds", agg["rounds"], "—"],
                ["Model tokens (QA + fix)", tokens, pricing.fmt_usd(agg["usd"])],
                ["Agent session seconds", f"{agg['session_seconds']}s", "included above"],
                ["Loop wall-clock", f"{agg['seconds']}s", "—"],
            ],
        )
    ]
    if not agg["tokens_known"]:
        lines += [
            "",
            f"_{agg['unknown_fix_token_rounds']} round(s) ran the fix agent and reported "
            "no token usage, so the token count above is a floor rather than a total._",
        ]
    if agg["usd"] is None:
        lines += [
            "",
            "_The dollar total is **unpriced**: at least one meter had no entry in the "
            "price table. Units above are still exact._",
        ]
    return "\n".join(lines)


def render(rounds: list, decision, labels: dict | None = None) -> str:
    """The whole comment. `decision` is a convergence.Decision."""
    icon = ICON.get(decision.state, "")
    parts = [
        MARKER,
        f"## {icon} Convergence loop — {decision.state} "
        f"({len(rounds)} round{'s' if len(rounds) != 1 else ''})",
        "",
        # The reasons are written to read as the second half of "<state>: ...",
        # which is how they appear in the job log. As a standalone paragraph
        # under a headline they need the capital.
        decision.reason[:1].upper() + decision.reason[1:],
        "",
        "### Rounds",
        "",
        _rounds_table(rounds),
    ]

    # The ledger is a history, and on a round that is about to be followed by
    # another one it would be a history of nothing. PLAN.md asks for it "on the
    # final round" for exactly that reason.
    if decision.stop:
        parts += [
            "",
            "### Reconciliation",
            "",
            _ledger_table(conv.reconcile(rounds, labels)),
        ]

    parts += ["", "### Cost", "", _cost_table(rounds), "", "### What happens next", "", NEXT.get(decision.state, "")]
    return "\n".join(parts)

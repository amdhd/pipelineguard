"""
The PR summary.

NAMED summary.py, NOT report.py -- `agents/qa/harness/report.py` already claims
`report`. See the note at the top of harness.py.

PLAN.md Phase 2: "post a summary table of what was patched, what was skipped,
and why", plus the fourth cost meter. The reviewer of an agent's PR needs one
thing above all -- to know what the agent did NOT do -- because that is the part
they cannot see in the diff.

So skips are not a footnote here. They are a first-class table with a reason
column, and a run that patched nothing still renders a full report rather than
an empty one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa" / "harness"))

import pricing  # noqa: E402


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def cost_line(budget, model: str, prices: dict | None = None) -> str:
    """
    The fourth meter, reported the way the other three are.

    Units first and always correct; dollars second and explicitly an estimate.
    An unknown model renders "unpriced", never $0.00 -- PRICING.md's honesty
    rule, which exists because a silent zero reads as "this run was free" when
    it means "nobody told me the price".
    """
    prices = prices if prices is not None else pricing.load_prices()
    usd = pricing.model_cost_usd(
        model,
        budget.input_tokens,
        budget.output_tokens,
        prices,
        cache_read=budget.cache_read,
        cache_write=budget.cache_write,
    )
    return (
        f"**Fix model:** `{model}` — {budget.calls} call(s), "
        f"{budget.input_tokens:,} in / {budget.output_tokens:,} out "
        f"({budget.spent:,} of {budget.token_budget:,} budgeted) — "
        f"{pricing.fmt_usd(usd)}"
    )


def render(result: dict, *, budget=None, model: str = "", prices: dict | None = None) -> str:
    """Render the whole summary. `result` is what main.run assembles."""
    applied = result.get("applied", [])
    skipped = result.get("skipped", [])
    excluded = result.get("excluded", [])

    verdict = (
        f"Patched {len(applied)} edit(s) across "
        f"{len({a['path'] for a in applied})} file(s)."
        if applied
        else "**No patches applied.**"
    )

    parts = [
        "## Bug-fix agent",
        "",
        verdict,
        "",
        "### Patched",
        "",
        _table(
            ["Finding", "File", "Lines", "Why"],
            [[a["finding_id"], f"`{a['path']}`", a["lines"], a["rationale"]] for a in applied],
        ),
        "",
        "### Skipped",
        "",
        _table(
            ["Finding", "Reason"],
            [[s.get("finding_id", "—"), s["reason"]] for s in skipped],
        ),
    ]

    if excluded:
        # Recorded because PLAN.md asks for it by name: a file that matched and
        # did not fit is the difference between "the agent could not find it"
        # and "the agent was not shown it", and only one of those is a bug.
        parts += [
            "",
            "### Not shown to the model (context budget)",
            "",
            _table(
                ["File", "Reason"],
                [[f"`{e['path']}`", e["reason"]] for e in excluded],
            ),
        ]

    if budget is not None:
        parts += ["", "---", "", cost_line(budget, model, prices)]

    parts += [
        "",
        "_One pass, no retry. Every patch above compiled and passed the test "
        "suite before this PR was opened; nothing here has been reviewed by a "
        "human yet._",
    ]
    return "\n".join(parts)


def exit_code(result: dict) -> int:
    """
    0 when the run did what it was asked, 1 when it could not.

    Applying nothing is NOT a failure -- "every finding was honestly skipped" is
    a valid outcome and the prompt is written to encourage it. What fails is the
    harness itself breaking: an invalid response, an exhausted budget, a batch
    refused by the caps.
    """
    return 1 if result.get("error") else 0

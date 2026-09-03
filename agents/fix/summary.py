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


def budget_block(budget, model: str) -> dict:
    """
    The same meters as `cost_line`, for the MACHINE-READABLE result.

    Units and the model name; no dollars. Converting units to money happens
    wherever the price table lives, and doing it in two places is how the two
    disagree -- the rule agent.py states for the QA half, which holds across
    programs and not just across the runtime boundary.

    This exists for Phase 3. The convergence loop enforces a CUMULATIVE token
    budget across rounds, and it cannot enforce what the artefacts do not carry:
    the QA half's tokens have always been in the findings JSON, while this half
    rendered its own into a markdown line and dropped them. A loop reading only
    the half it could see would compare a floor against its cap and call it a
    total.
    """
    return {
        "model": model,
        "calls": budget.calls,
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "cache_read": budget.cache_read,
        "cache_write": budget.cache_write,
        "spent": budget.spent,
        "token_budget": budget.token_budget,
    }


def _origin_block(origin: dict) -> list[str]:
    """
    The D-4 provenance banner for a fix PR.

    The fix leg only means anything in relation to the origin QA run it answers,
    so the body says which run that is and which fingerprints the patches claim
    to address -- the exact set the fix PR's QA run will reconcile as FIXED.
    """
    pr = origin.get("pr")
    repo = origin.get("repo")
    ref = f"{repo}#{pr}" if repo else f"#{pr}"
    fps = origin.get("applied_fingerprints") or []
    lines = [
        "### Origin",
        "",
        f"This PR is the **fix leg** of QA on {ref}. It reconciles the origin "
        f"findings at `{origin.get('findings_key', '?')}`: merge this, and the "
        "origin PR's next QA run re-verifies the fingerprints below as FIXED.",
        "",
        f"Applied fingerprints ({len(fps)}):",
    ]
    if fps:
        lines += [f"- `{fp}`" for fp in fps]
    else:
        lines.append("_none -- nothing was patched in this run._")
    return lines


def render(result: dict, *, budget=None, model: str = "", prices: dict | None = None) -> str:
    """Render the whole summary. `result` is what main.run assembles."""
    applied = result.get("applied", [])
    skipped = result.get("skipped", [])
    excluded = result.get("excluded", [])

    errors = result.get("errors") or ([result["error"]] if result.get("error") else [])

    verdict = (
        f"Patched {len(applied)} edit(s) across "
        f"{len({a['path'] for a in applied})} file(s)."
        if applied
        else "**No patches applied.**"
    )
    if errors and applied:
        # A partially failed run must not read as a clean one. The patches below
        # are real and gated; the failures are real too, and a reviewer who is
        # not told about them will assume the findings list was fully addressed.
        verdict += (
            f" **{len(errors)} finding(s) failed** and are listed under Skipped —"
            " this run is PARTIAL, not complete."
        )

    parts: list[str] = []
    origin = result.get("origin")
    if origin:
        # The origin banner leads the body -- the fix loop only reads as one
        # loop when a reader of the fix PR is told which QA run it answers.
        parts += _origin_block(origin)
        parts += ["", "---", ""]
    parts += [
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
    0 means "the workflow may proceed to the gate", 1 means "there is nothing
    usable here". It is NOT a report card on the findings.

    THIS USED TO CONFLATE TWO DIFFERENT THINGS, and the bug was expensive.
    Any single finding's failure set `result["error"]`, which returned 1, which
    failed the workflow step, which skipped the gate and the commit -- so a run
    where four findings produced good, compiling patches and the fifth hit an
    unparseable reply threw all four away on a runner that was then destroyed.
    Reproduced before the fix: exit code 1 with a correct patch sitting on disk.

    So the question this answers is narrow: did anything get applied? If yes,
    proceed -- the gate is what decides whether the patches are good, and it
    cannot decide anything about work it never sees. Per-finding failures are
    reported in the summary, loudly, and do not veto the patches that worked.

    Applying nothing is still not a failure on its own: "every finding was
    honestly skipped" is a valid outcome the prompt actively encourages. It is a
    failure only when nothing was applied AND something broke.
    """
    if result.get("applied"):
        return 0
    return 1 if (result.get("errors") or result.get("error")) else 0

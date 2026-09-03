"""
Rendering the PR comment.

The comment is the product. Findings that a reviewer cannot act on are findings
that did not happen, so this module cares about two things the rest of the
harness does not:

  * Evidence must be REACHABLE. The reports bucket is block-public-access, so a
    bare S3 key is unopenable. The agent returns presigned URLs; they get
    rendered as links.
  * Cost must be legible and honest -- all three meters, and "unpriced" where a
    price is genuinely unknown rather than a $0.00 that reads as free.
"""

from pricing import fmt_usd

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
SEVERITY_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "⚪"}

MARKER = "<!-- pipelineguard-qa-agent -->"

# Failure modes worth telling the reader what to DO about. A run that failed and
# explains nothing costs the same as one that failed and points somewhere.
_ERROR_HINTS = {
    "runtime_unavailable": (
        "The AgentCore runtime could not be invoked or returned an error. Check "
        "its CloudWatch log group — the cause is there, not in this workflow."
    ),
    "schema_violation": (
        "The agent ran but returned something that is not a valid findings "
        "object. That is a prompt bug, not an application bug."
    ),
    "invalid_runtime_response": (
        "The runtime returned a non-JSON body. Usually an unhandled exception "
        "inside the agent; its CloudWatch logs will have the traceback."
    ),
    "bad_payload": "The harness was invoked without a field the agent requires.",
    "unauthenticated": (
        "The agent never logged in, so it was looking at the login page rather "
        "than the application. Findings were discarded — an unauthenticated "
        "run that reports nothing is indistinguishable from a healthy app, and "
        "publishing it as a PASS would be worse than publishing nothing."
    ),
}


def _finding_block(f: dict) -> str:
    icon = SEVERITY_ICON.get(f["severity"], "")
    lines = [
        f"<details><summary>{icon} <b>{f['severity']}</b> — <code>{f['page']}</code> — {f['summary']}</summary>",
        "",
        f"**Expected:** {f['expected']}",
        "",
        f"**Actual:** {f['actual']}",
        "",
        f"**Evidence:** {f['evidence']}",
        "",
        "**Steps to reproduce:**",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(f.get("steps_to_reproduce", []), 1)]

    shot = f.get("screenshot")
    if isinstance(shot, dict) and shot.get("url"):
        # Inline, not a bare key. A reviewer should not have to go and fetch the
        # evidence for the finding they are being asked to judge.
        lines += ["", f"![{f['id']} evidence]({shot['url']})"]
    elif isinstance(shot, dict) and shot.get("key"):
        lines += ["", f"_Screenshot at `{shot['key']}` (no presigned URL returned)._"]

    src = f.get("suspected_source")
    lines += ["", f"**Suspected source:** {f'`{src}`' if src else '_not identified_'}", "", "</details>"]
    return "\n".join(lines)


def _cache_line(cost: dict) -> str | None:
    """
    Whether prompt caching actually worked, in one line.

    Worth its own row because it is invisible otherwise and expensive when it
    silently stops: the agent re-sends its whole history every turn, so a run
    whose cache never matches pays full input price for the same tokens dozens
    of times. The token counts say what happened; the hit rate says whether it
    was supposed to.
    """
    rate = cost.get("cache_hit_rate")
    if rate is None or not cost.get("total_input_tokens"):
        return None
    if not cost.get("cache_read_tokens") and not cost.get("cache_write_tokens"):
        return "_Prompt cache: not used on this run._"
    if not cost.get("cache_read_tokens"):
        return (
            f"_Prompt cache: **0% hit rate** — {cost['cache_write_tokens']:,} tokens were "
            "written to cache and none read back. Something volatile is changing the "
            "prompt prefix between turns; this run cost several times what it should._"
        )
    return (
        f"_Prompt cache: {rate:.0%} of input served from cache "
        f"({cost['cache_read_tokens']:,} read, {cost['cache_write_tokens']:,} written)._"
    )


def _cost_table(cost: dict) -> str:
    # Uncached input only, matching how Bedrock reports it -- the cached tokens
    # are on the line below, at their own rates.
    tokens = f"{cost['input_tokens']:,} in / {cost['output_tokens']:,} out"
    if cost.get("cache_read_tokens") or cost.get("cache_write_tokens"):
        tokens += f" (+{cost['cache_read_tokens']:,} cached)"
    rows = [
        "| Meter | Units | Estimated |",
        "|---|---|---|",
        f"| Model inference (`{cost['model']}`) | {tokens} | {fmt_usd(cost['model_usd'])} |",
        f"| AgentCore runtime session | {cost['session_seconds']}s | {fmt_usd(cost['runtime_usd'])} |",
        f"| Browser session | {cost['session_seconds']}s | {fmt_usd(cost['browser_usd'])} |",
        f"| **Total** | {cost['turns']} turns | **{fmt_usd(cost['estimated_total_usd'])}** |",
    ]
    notes = [f"_Excludes: {', '.join(cost['excludes'])}._"]
    cache_line = _cache_line(cost)
    if cache_line:
        notes.append(cache_line)
    if cost["unpriced"]:
        notes.append(
            f"_`{cost['model']}` is not in the price table, so inference is unpriced and the "
            "total is incomplete. Token counts above are measured and exact. "
            "See `agents/qa/harness/PRICING.md`._"
        )
    return "\n".join(rows + [""] + notes)


# Statuses a prior finding can carry after a re-run. Produced upstream by
# `converge.verify_report`; the renderer only maps them, so the comment and the
# ledger can never disagree about vocabulary.
_REVERIFY_STATUS = {
    "still failing": "🔴 **STILL FAILING**",
    "fixed": "✅ **FIXED**",
    "not reproduced": "⚪ NOT REPRODUCED",
    "unverified": "⏸ **UNVERIFIED**",
}


def render_reverify(rows: list) -> str:
    """
    The ``🔁 Prior findings re-verified`` block.

    ``rows`` are reconciliation rows from ``converge.verify_report`` -- one per
    finding the previous report raised, each carrying ``fingerprint``,
    ``severity``, ``page``, ``summary`` and a ``status``. Rendered as a table
    because that is the one question a fix loop turns on: is the defect we
    reported before still there, or is it gone?

    The status column is the upstream vocabulary, verbatim. Absence never reads
    as fixed -- only ``fixed`` says fixed, and it only appears when a fix is
    known to have intervened between the two measurements.
    """
    if not rows:
        return ""
    ordered = sorted(
        rows,
        key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["page"], r["summary"]),
    )
    lines = [
        "### 🔁 Prior findings re-verified",
        "",
        "Re-verified against the run above. A defect that no longer appears is "
        "`fixed` only when a fix is known to have been applied since the last "
        "report; otherwise it is `not reproduced` -- one clean run does not "
        "erase a reported defect on its own.",
        "",
        "| Status | Page | Finding |",
        "|---|---|---|",
    ]
    for r in ordered:
        status = _REVERIFY_STATUS.get(r["status"], f"**{r['status'].upper()}**")
        summary = r["summary"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {status} | `{r['page']}` | {summary} |")
    if any(r["status"] == "unverified" for r in ordered):
        lines += [
            "",
            "_`UNVERIFIED` means this run failed or stopped before it could "
            "re-test the route that finding lives on. A run that never re-tested "
            "a defect is not evidence the defect is gone._",
        ]
    return "\n".join(lines)


def render_board(rows: list) -> str:
    """
    The ``🧾 Final reconciliation board`` that closes an origin PR (D-4).

    ``rows`` are the board rows built by the harness -- one per finding the
    origin QA raised, carrying ``fingerprint``/``severity``/``page``/``summary``,
    a machine ``status`` (``still failing`` | ``fixed`` | ``not reproduced``),
    a ``source`` (this run vs the fix PR's fix-verdict.json) and a ``label``
    slot that is null until a human writes it.

    The header is "Final" ONLY when at least one row is actually fixed by a fix
    leg. A clean re-run with no fix chain reconciles but closes nothing, and a
    comment that said otherwise would be the benchmark's prose-only board all
    over again -- a closing table that overclaims what it shows.
    """
    if not rows:
        return ""
    ordered = sorted(
        rows,
        key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["page"], r["summary"]),
    )
    any_fixed = any(r["status"] == "fixed" for r in ordered)
    fixed_n = sum(1 for r in ordered if r["status"] == "fixed")
    if any_fixed:
        header = "### 🧾 Final reconciliation board"
        lede = (
            f"Every finding the origin QA raised is accounted for: **{fixed_n} "
            "fixed** by the fix leg; the rest are still failing, were not "
            "reproduced, or await a human verdict."
        )
    else:
        header = "### 🧾 Reconciliation board"
        lede = (
            "A clean re-run of the origin report, but no finding was fixed by a "
            "fix leg -- absence alone is not a fix, so nothing here is closed. "
            "Every row still needs a human verdict."
        )
    lines = [
        header,
        "",
        lede,
        "",
        "| Status | Page | Finding | Human label |",
        "|---|---|---|---|",
    ]
    for r in ordered:
        status = _REVERIFY_STATUS.get(r["status"], f"**{r['status'].upper()}**")
        summary = r["summary"].replace("|", "\\|").replace("\n", " ")
        label = r.get("label") or "_needs a human_"
        lines.append(f"| {status} | `{r['page']}` | {summary} | {label} |")
    if any(r.get("label") is None for r in ordered):
        lines += [
            "",
            "_Unlabelled rows are never counted as resolved: `true-positive`, "
            "`false-positive` and `by-design` are human judgements made "
            "downstream, and agent output is not evidence for them._",
        ]
    if any(r.get("source") == "fix-verdict" for r in ordered):
        lines += [
            "",
            "_Fixed rows are marked from the fix PR's QA verdict "
            "(`fix-verdict.json` in this report's namespace), reconciled against "
            "this run._",
        ]
    return "\n".join(lines)


def render_unmatched(findings: list) -> str:
    """
    The ``⚠️ New findings not in the origin report`` note (D-4).

    Findings the previous report never raised. Each is a new defect or a
    regression introduced by this branch -- the PR path has no sound way to mint
    a fabricated ``regressed`` origin row, so regression risk surfaces here, and
    a HIGH/CRITICAL one still fails the check by the ordinary exit-code rule.
    """
    if not findings:
        return ""
    ordered = sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f.get("page", ""), f.get("summary", "")),
    )
    blocking = [f for f in ordered if f["severity"] in ("CRITICAL", "HIGH")]
    caveat = (
        "**One or more are HIGH/CRITICAL and fail this check.**"
        if blocking
        else "None is blocking on its own, but each still wants a look."
    )
    lines = [
        "### ⚠️ New findings not in the origin report",
        "",
        f"These appeared in this run and have no counterpart in the report it "
        f"re-verified against. Each is either a new defect or a regression "
        f"introduced by this branch. {caveat}",
        "",
        "| Severity | Page | Finding |",
        "|---|---|---|",
    ]
    for f in ordered:
        icon = SEVERITY_ICON.get(f["severity"], "")
        summary = f["summary"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {icon} {f['severity']} | `{f['page']}` | {summary} |")
    return "\n".join(lines)


def render(
    findings: dict,
    *,
    runner_minutes: int | None = None,
    reverify_rows: list | None = None,
    board_rows: list | None = None,
    unmatched: list | None = None,
) -> str:
    """Render the full comment. Always leads with the verdict."""
    from pricing import summarise

    if findings.get("error"):
        parts = [
            MARKER,
            "## 🚫 QA agent — run failed",
            "",
            f"**`{findings['error']}`** — {findings.get('detail', 'no detail')}",
            "",
            _ERROR_HINTS.get(findings["error"], ""),
            "",
            "No findings are reported. A failed run is not a passing run: the agent "
            "either could not start or returned something that did not match the "
            "findings schema, and unparsed output is never presented as bugs.",
        ]
        if reverify_rows:
            # A run that errored re-measured nothing, so every prior finding is
            # UNVERIFIED -- but that verdict only lands if the block is shown.
            parts += ["", render_reverify(reverify_rows)]
        return "\n".join(parts)

    cost = summarise(findings)
    items = sorted(findings.get("findings", []), key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    blocking = [f for f in items if f["severity"] in ("CRITICAL", "HIGH")]

    if findings.get("incomplete"):
        headline = f"## ⚠️ QA agent — partial run ({findings.get('stop_reason', 'budget exhausted')})"
    elif blocking:
        headline = f"## ❌ QA agent — FAIL ({len(blocking)} blocking)"
    else:
        # Derived from SEVERITIES, not from the model's own "overall". A real run
        # returned overall=FAIL while reporting a single MEDIUM, contradicting
        # the rubric it had just been given. exit_code already ignores that field
        # for the same reason; the headline should agree with the exit code.
        headline = "## ✅ QA agent — PASS"

    parts = [MARKER, headline, ""]

    # THE TARGET URL IS DELIBERATELY ABSENT.
    #
    # PLAN.md 1e: the tunnel is an unauthenticated public URL and there is "no
    # reason to write it anywhere durable -- not into the PR comment, not into
    # the findings JSON, not into logs." This used to print it, on a public
    # repo, which is as durable as it gets. The tunnel dies with the job, so the
    # link is stale by the time anyone reads it -- but a rule this repo states in
    # its own plan should not be broken by its own reporting.
    parts += [f"{findings.get('pages_tested', 0)} routes tested", ""]

    if findings.get("auth_probe") == "not_configured":
        # Auth was never measured, so the run cannot tell a healthy app from a
        # login page the agent never got past. That caveat belongs ABOVE the
        # findings, where a PASS headline cannot hide it. Distinct from the
        # `unauthenticated` error: this run is not known to have failed login,
        # it just never checked.
        parts += [
            "**Auth was not verified.** No auth token key was configured, so the "
            "run could not check whether the agent ever got past the login page. "
            "Every private route redirects when unauthenticated — read any "
            "finding (or the absence of one) with that in mind.",
            "",
        ]

    if not items and findings.get("incomplete") and not findings.get("report_salvaged"):
        # An empty list on a truncated run is not the same claim as an empty
        # list on a complete one, and must not read like it.
        parts += [
            "**No findings could be salvaged.** The run stopped before the agent "
            "produced a report, so this is not evidence that the application is "
            "healthy -- it is an absence of evidence either way.",
            "",
        ]
    elif not items:
        parts += ["No findings. The agent explored the route list and observed nothing reportable.", ""]
    else:
        counts = {s: sum(1 for f in items if f["severity"] == s) for s in SEVERITY_ORDER}
        summary = " · ".join(
            f"{SEVERITY_ICON[s]} {counts[s]} {s.lower()}" for s in SEVERITY_ORDER if counts[s]
        )
        parts += [summary, ""]
        parts += [_finding_block(f) for f in items]
        parts += [""]

    if board_rows:
        # The board is the closing ledger and a superset of the plain re-verify
        # table, so it replaces it -- a clean origin run does not show both.
        parts += ["", render_board(board_rows), ""]
    elif reverify_rows:
        parts += ["", render_reverify(reverify_rows), ""]
    if unmatched:
        # Regression risk surfaces as a note, never as a fabricated origin row.
        parts += ["", render_unmatched(unmatched), ""]

    parts += ["### Cost", "", _cost_table(cost)]
    if runner_minutes is not None:
        # Free on a public repo. Reported anyway: a zero you can defend is worth
        # more than an omitted row, and if the repo ever goes private this line
        # is already here and starts costing.
        parts += ["", f"_Runner minutes: {runner_minutes} (billed: $0.00 on a public repo)._"]

    return "\n".join(parts)


def exit_code(findings: dict) -> int:
    """
    0 pass, 1 blocking findings, 2 the run itself failed.

    Distinct codes because they want different responses: blocking findings are
    about the application, a failed run is about the pipeline, and collapsing
    them would let a broken harness read as a clean app.
    """
    if findings.get("error"):
        return 2
    if findings.get("incomplete"):
        return 2
    if any(f["severity"] in ("CRITICAL", "HIGH") for f in findings.get("findings", [])):
        return 1
    return 0

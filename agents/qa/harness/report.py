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


def _cost_table(cost: dict) -> str:
    rows = [
        "| Meter | Units | Estimated |",
        "|---|---|---|",
        f"| Model inference (`{cost['model']}`) | {cost['input_tokens']:,} in / {cost['output_tokens']:,} out | {fmt_usd(cost['model_usd'])} |",
        f"| AgentCore runtime session | {cost['session_seconds']}s | {fmt_usd(cost['runtime_usd'])} |",
        f"| Browser session | {cost['session_seconds']}s | {fmt_usd(cost['browser_usd'])} |",
        f"| **Total** | {cost['turns']} turns | **{fmt_usd(cost['estimated_total_usd'])}** |",
    ]
    notes = [f"_Excludes: {', '.join(cost['excludes'])}._"]
    if cost["unpriced"]:
        notes.append(
            f"_`{cost['model']}` is not in the price table, so inference is unpriced and the "
            "total is incomplete. Token counts above are measured and exact. "
            "See `agents/qa/harness/PRICING.md`._"
        )
    return "\n".join(rows + [""] + notes)


def render(findings: dict, *, target_url: str | None = None, runner_minutes: int | None = None) -> str:
    """Render the full comment. Always leads with the verdict."""
    from pricing import summarise

    if findings.get("error"):
        return "\n".join(
            [
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
        )

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

    if target_url:
        parts += [f"Target: `{target_url}` · {findings.get('pages_tested', 0)} routes tested", ""]

    if not items:
        parts += ["No findings. The agent explored the route list and observed nothing reportable.", ""]
    else:
        counts = {s: sum(1 for f in items if f["severity"] == s) for s in SEVERITY_ORDER}
        summary = " · ".join(
            f"{SEVERITY_ICON[s]} {counts[s]} {s.lower()}" for s in SEVERITY_ORDER if counts[s]
        )
        parts += [summary, ""]
        parts += [_finding_block(f) for f in items]
        parts += [""]

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

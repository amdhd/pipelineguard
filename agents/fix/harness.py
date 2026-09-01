"""
Bug-fix harness CLI -- runs in the GitHub runner, beside the checked-out target.

NAMED harness.py, NOT main.py. `agents/qa/harness/main.py` already claims the
module name `main` in the shared test session, and --import-mode=importlib fixes
that for identically-named TEST files only -- it does nothing for source
modules. Naming this main.py did not fail here; it silently broke the QA
harness's OWN tests, which is the worse direction for a collision to fail in.
The same applies to summary.py, which would otherwise have been report.py.

WHAT THIS PROGRAM IS
--------------------
Findings JSON in, edited working tree out, plus a summary for the PR. It does
NOT commit, push, or open the PR -- the workflow does that, with the GitHub App
token, after the compile-and-test gate has passed. Keeping the write to git out
of this program is what lets the gate sit between the edit and the commit, which
is the whole point of the gate.

ORDER OF OPERATIONS, AND WHY
----------------------------
For each finding: select files -> ask the model -> validate -> plan the edits ->
apply. The model is asked once per finding and never sees a file outside the
allow-list. Nothing is written until a finding's whole batch has passed the
caps, and a finding that cannot be located is skipped with its reason rather
than attempted against whatever files happened to score highest.
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import edits as edit_rules  # noqa: E402
import fix_schema  # noqa: E402
import model as fix_model  # noqa: E402
import summary as fix_summary  # noqa: E402
import sources  # noqa: E402

logger = logging.getLogger(__name__)

# The QA schema already defines these two as BLOCKING -- the severities that make
# a run FAIL and gate a PR. Attempting exactly them is therefore a principled
# default rather than an arbitrary line: the fix agent addresses what blocks, and
# a human decides about the rest.
#
# It is also the cost control. Every finding attempted costs a model call
# (~$0.11 measured) and one of the five slots the token budget pays for, so a
# handful of cosmetic LOWs can consume a whole run's budget and crowd out the
# defect that actually matters.
DEFAULT_SEVERITIES = "CRITICAL,HIGH"


def _load_findings(path: Path) -> tuple[list[dict], str | None]:
    """Returns (findings, observed_at_commit). The commit is None when unstamped."""
    payload = json.loads(path.read_text())
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("findings JSON does not carry a 'findings' list")
    observed = payload.get("observed_at_commit")
    return findings, observed if isinstance(observed, str) else None


def _head_commit(root: Path) -> str | None:
    """The checkout's HEAD, or None when `root` is not a git repository."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def staleness(observed: str | None, head: str | None) -> str | None:
    """
    Why these findings must not be acted on, or None if they may be.

    A FINDINGS JSON OUTLIVES THE CODE IT DESCRIBES, and nothing else in this
    harness notices. Run 33137979741 observed two defects on `qa-corpus-1`;
    replayed three days later against `main`, one had already been fixed and the
    other never existed there. The agent produced confident, compiling, wrong
    patches for both.

    The "old_string not found" check in edits.py does not catch this. It only
    stops an agent that tries the exact stale edit -- one that adapts to the
    code in front of it will happily fix a defect that is not there, which is
    what happened. So the guard has to sit above the model, not below it.

    Unstamped findings are ALLOWED, with a warning, because refusing them would
    break every report written before this field existed. Present-and-different
    is refused, because that is the case we have actually been burned by.
    """
    if observed is None:
        return None
    if head is None:
        return None
    if observed != head:
        return (
            f"findings were observed at {observed[:12]}, the checkout is at "
            f"{head[:12]}. A report is only about the code it was written "
            "against; pass --allow-stale-findings to override deliberately."
        )
    return None


def run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    root = Path(args.repo).resolve()
    findings, observed = _load_findings(Path(args.findings))

    # Created BEFORE the staleness check so that every exit from this function
    # reports token usage, including the ones that spent nothing. Phase 3's
    # cumulative budget cannot tell "no tokens" from "no answer" unless the zero
    # is written down, and it stops the loop when it cannot tell.
    budget = fix_model.Budget(args.token_budget)

    stale = staleness(observed, _head_commit(root))
    if stale and not args.allow_stale_findings:
        result = {
            "applied": [], "excluded": [], "errors": [stale],
            "skipped": [{"finding_id": f.get("id", "?"), "reason": stale} for f in findings],
            "budget": fix_summary.budget_block(budget, args.model),
        }
        rendered = fix_summary.render(result, model=args.model)
        if args.summary_out:
            Path(args.summary_out).write_text(rendered)
        else:
            print(rendered)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(result, indent=2, default=str))
        return fix_summary.exit_code(result)
    if observed is None:
        logger.warning(
            "findings carry no observed_at_commit; staleness could not be checked"
        )

    if args.severities:
        wanted = {s.strip().upper() for s in args.severities.split(",")}
        findings = [f for f in findings if str(f.get("severity", "")).upper() in wanted]

    # Beyond the cap, findings are SKIPPED WITH A REASON. Silently dropping them
    # would make a run's coverage depend on a number nobody was shown.
    attempted, deferred = findings[: args.max_findings], findings[args.max_findings :]

    bedrock = None if args.dry_run else fix_model.client(args.region)

    result: dict = {"applied": [], "skipped": [], "excluded": [], "errors": []}
    for finding in deferred:
        result["skipped"].append({
            "finding_id": finding.get("id", "?"),
            "reason": f"beyond --max-findings ({args.max_findings}) for this run",
        })

    # Threaded through edit_rules.plan so the file and line caps bound the RUN,
    # not each finding separately -- five findings under a five-file cap could
    # otherwise produce a twenty-five-file PR.
    applied_files: set[str] = set()
    applied_lines = 0

    for finding in attempted:
        fid = finding.get("id", "?")
        selection = sources.select(finding, root)
        result["excluded"].extend(selection["excluded"])

        if selection["reason"]:
            result["skipped"].append({"finding_id": fid, "reason": selection["reason"]})
            continue

        if args.dry_run:
            # Everything except the model call. Proves selection and the caps on
            # a real tree without spending a token, which is what makes the
            # smoke test's first run free.
            shown = ", ".join(f["path"] for f in selection["files"])
            result["skipped"].append({"finding_id": fid, "reason": f"dry run; would show: {shown}"})
            continue

        try:
            proposal = fix_model.propose(
                finding, selection, bedrock=bedrock, budget=budget,
                model=args.model, max_tokens=args.max_output_tokens,
            )
        except fix_model.BudgetExhausted as e:
            result["skipped"].append({"finding_id": fid, "reason": str(e)})
            result["errors"].append(str(e))
            break
        except fix_schema.FixSchemaError as e:
            result["skipped"].append({"finding_id": fid, "reason": f"invalid response: {e}"})
            result["errors"].append(str(e))
            # Keep the model's full text in the machine-readable result, which
            # the workflow uploads as an artefact. Without it, diagnosing a
            # parse failure means paying for the call again -- run 33408195295
            # cost $0.09 and left a 300-character excerpt behind.
            raw = getattr(e, "raw", None)
            if raw:
                result.setdefault("raw_responses", {})[fid] = raw
            continue

        result["skipped"].extend(
            {"finding_id": s.get("finding_id", fid), "reason": s["reason"]}
            for s in proposal["skipped"]
        )

        rationales = {
            (e["file"], e["old_string"]): (e.get("finding_id", fid), e.get("rationale", ""))
            for e in proposal["edits"]
        }
        planned = edit_rules.plan(
            proposal["edits"],
            root,
            applied_files=frozenset(applied_files),
            applied_lines=applied_lines,
        )
        result["skipped"].extend(
            {"finding_id": s["edit"].get("finding_id", fid), "reason": s["reason"]}
            for s in planned["skip"]
        )
        if planned["batch_error"]:
            result["errors"].append(planned["batch_error"])

        for item in edit_rules.write(planned):
            found_id, rationale = rationales.get(
                (item["edit"]["file"], item["edit"]["old_string"]), (fid, "")
            )
            applied_files.add(item["path"])
            applied_lines += item["lines"]
            result["applied"].append(
                {
                    "finding_id": found_id,
                    "path": item["path"],
                    "lines": item["lines"],
                    "rationale": rationale,
                }
            )

    result["budget"] = fix_summary.budget_block(budget, args.model)

    rendered = fix_summary.render(result, budget=budget, model=args.model)
    if args.summary_out:
        Path(args.summary_out).write_text(rendered)
    else:
        print(rendered)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2, default=str))

    return fix_summary.exit_code(result)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Propose and apply fixes for QA findings.")
    p.add_argument("--findings", required=True, help="Findings JSON from the QA run")
    p.add_argument("--repo", default=".", help="Root of the checked-out target repo")
    p.add_argument("--region", default=fix_model.DEFAULT_REGION)
    p.add_argument("--model", default=fix_model.DEFAULT_MODEL)
    p.add_argument("--token-budget", type=int, default=fix_model.DEFAULT_TOKEN_BUDGET)
    p.add_argument(
        "--max-findings",
        type=int,
        default=fix_model.DEFAULT_MAX_FINDINGS,
        help="Findings to attempt in one run. The token budget is derived from this, "
        "so raising it without raising --token-budget will exhaust the budget "
        "mid-run. Findings beyond the cap are reported as skipped.",
    )
    p.add_argument("--max-output-tokens", type=int, default=4096)
    p.add_argument(
        "--severities",
        default=DEFAULT_SEVERITIES,
        help=f"Comma-separated severities to attempt. Default: {DEFAULT_SEVERITIES}. "
        "Pass 'CRITICAL,HIGH,MEDIUM,LOW' to attempt everything.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Select files and enforce every guardrail, but make no model call and "
        "write nothing. Costs nothing and proves the selection half on a real tree.",
    )
    p.add_argument(
        "--allow-stale-findings",
        action="store_true",
        help="Act on findings observed at a different commit than the checkout. Off by "
        "default: a report is only about the code it was written against.",
    )
    p.add_argument("--summary-out", help="Write the PR summary here instead of stdout")
    p.add_argument("--json-out", help="Write the machine-readable result here")
    return p


def main(argv=None) -> int:  # pragma: no cover -- thin CLI wrapper
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

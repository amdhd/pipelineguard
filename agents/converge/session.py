"""
The convergence loop's CLI -- one invocation per round, from the workflow.

NAMED session.py, NOT main.py or harness.py: both are taken, and this program is
not a harness in the sense the other two are. It invokes nothing and spends
nothing. It is called AFTER a round's QA and fix steps have run, is handed what
they produced, and answers one question -- another round, or stop?

THE STATE FILE IS THE LOOP'S MEMORY
-----------------------------------
GitHub Actions cannot express a while-loop over jobs, so the loop is a sequence
of steps in one job and the rounds have to remember each other somehow. That
memory is a JSON file passed forward on disk, and keeping it a FILE rather than
a step output matters: it is uploadable as an artefact, diffable when a decision
looks wrong, and replayable locally against the same code that made the call.

A round is appended, never rewritten. The convergence check is a claim about a
sequence, so a state file that lost a round would silently change the answer.

EXIT CODES
----------
  0  PASS, or CONTINUE -- the loop is healthy, whether or not it is finished
  1  a stop that is not a PASS -- there are blocking findings or a breached cap
  2  this program failed

The workflow branches on the `decision` and `stop` outputs rather than on the
exit code, because a step that exits non-zero mid-loop would abort the job
before the comment it just wrote could be posted. The codes are here for the
human running it over two saved findings files, which is also how its own tests
drive it.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import convergence as conv  # noqa: E402
import ledger  # noqa: E402


class StateError(ValueError):
    """
    A JSON input the session was handed -- the state file, this round's findings,
    or the fix result -- could not be read.

    The session answers "another round, or stop?" from these files, so an
    unreadable one is a program failure (exit 2), reported cleanly rather than
    as a traceback. The state file gets special attention: a CORRUPT state file
    is not a missing one. Treating it as round 1 would silently reset the
    cumulative caps the loop exists to enforce, and spend money the loop was not
    authorized to spend.
    """


def load_state(path: Path, budgets: dict) -> dict:
    """The state so far, or a fresh one. A missing file is round 1, not an error."""
    if not path.exists():
        return {"budgets": budgets, "rounds": [], "decisions": []}
    text = path.read_text()
    if not text.strip():
        # An empty file carries no history, which is what a missing one carries.
        return {"budgets": budgets, "rounds": [], "decisions": []}
    try:
        state = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateError(
            f"{path.name} exists but is not valid JSON ({e}). Refusing to "
            "guess rather than silently restarting the loop from round 1."
        ) from e
    # Budgets are re-read from the flags every round rather than trusted from
    # the file. The caps belong to the workflow that is spending the money;
    # a state file that carried its own would let a stale artefact quietly
    # raise them.
    state["budgets"] = budgets
    return state


def _read_json(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    try:
        payload = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise StateError(f"{p.name} is not valid JSON ({e})") from e
    if not isinstance(payload, dict):
        # The session's contract is a JSON object on every input -- a findings
        # artefact, a fix-result artefact. Valid JSON of another shape (a list,
        # a string) would crash downstream in round_record with an attribute
        # error; refuse it here instead.
        raise StateError(
            f"{p.name} does not carry a JSON object (found {type(payload).__name__})"
        )
    return payload


def run(args) -> int:
    budgets = {
        "max_rounds": args.max_rounds,
        "token_budget": args.token_budget,
        "wall_clock_seconds": args.wall_clock_seconds,
        "round_timeout_seconds": args.round_timeout_seconds,
    }
    state_path = Path(args.state)
    try:
        state = load_state(state_path, budgets)
        findings = _read_json(args.findings)
        fix_result = _read_json(args.fix_result)
    except StateError as e:
        # A clean exit-2, not a traceback: the caller needs to see why, and the
        # loop must not proceed -- or be restarted -- on inputs it cannot read.
        print(f"converge session: {e}", file=sys.stderr)
        return 2

    # The round number comes from the state, not from a flag. A workflow that
    # passed the wrong one -- easy, in a loop written in YAML -- would compare a
    # round against itself and report a stall.
    number = len(state["rounds"]) + 1
    state["rounds"].append(
        conv.round_record(number, findings, fix_result, seconds=args.round_seconds)
    )

    decision = conv.decide(state["rounds"], state["budgets"])
    state["decisions"].append({"round": number, **decision.as_dict()})

    labels = _read_json(args.labels).get("labels") if args.labels else None
    comment = ledger.render(state["rounds"], decision, labels)

    state_path.write_text(json.dumps(state, indent=2))
    if args.comment_out:
        Path(args.comment_out).write_text(comment)
    else:
        print(comment)

    # Two consumers, two formats. The workflow reads key=value from
    # $GITHUB_OUTPUT; a human reads the line on stderr, which stays out of the
    # comment when --comment-out is not given.
    if args.github_output:
        with open(args.github_output, "a") as fh:
            fh.write(f"decision={decision.state}\n")
            fh.write(f"stop={'true' if decision.stop else 'false'}\n")
            fh.write(f"round={number}\n")
    print(f"round {number}: {decision.state} — {decision.reason}", file=sys.stderr)

    if not decision.stop or decision.ok:
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Decide whether the QA loop runs another round.")
    p.add_argument("--state", required=True, help="Loop state JSON, created on round 1")
    p.add_argument("--findings", required=True, help="This round's QA findings JSON")
    p.add_argument("--fix-result", help="This round's agent-fix-result.json, if a fix ran")
    p.add_argument(
        "--round-seconds",
        type=int,
        default=0,
        help="Wall-clock this round took. The per-round cap is enforced by the "
        "workflow's step timeout; this is what lets the loop refuse to start "
        "another round after an overrun, and what puts the number in the report.",
    )
    p.add_argument(
        "--labels",
        help="A corpus JSON carrying human labels, in score.py's vocabulary. Without "
        "it the ledger's label column reads 'needs a human', which is the honest "
        "answer rather than a blank.",
    )
    p.add_argument("--max-rounds", type=int, default=conv.DEFAULT_BUDGETS["max_rounds"])
    p.add_argument("--token-budget", type=int, default=conv.DEFAULT_BUDGETS["token_budget"])
    p.add_argument(
        "--wall-clock-seconds", type=int, default=conv.DEFAULT_BUDGETS["wall_clock_seconds"]
    )
    p.add_argument(
        "--round-timeout-seconds",
        type=int,
        default=conv.DEFAULT_BUDGETS["round_timeout_seconds"],
    )
    p.add_argument("--comment-out", help="Write the rendered comment here instead of stdout")
    p.add_argument(
        "--github-output",
        help="Path to append decision/stop/round to, normally $GITHUB_OUTPUT",
    )
    return p


def main(argv=None) -> int:  # pragma: no cover -- thin CLI wrapper
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

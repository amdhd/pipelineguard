"""
Fold repeated QA runs into one findings JSON, by majority vote (P1.3).

The convergence loop runs QA K times per round because a single run is not a
measurement (EVIDENCE.md: run-to-run variance flips findings on identical
code). This program aggregates the K findings JSONs into the one file the fix
half and the convergence layer expect, so neither has to know about repeats.

Stdlib only, like the rest of converge/. The vote itself is
`convergence.aggregate_findings`; this is just the shell-facing entry point.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import convergence as conv  # noqa: E402


def run(args) -> int:
    runs = []
    for path in args.findings:
        try:
            payload = json.loads(Path(path).read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"converge aggregate: {path}: {e}", file=sys.stderr)
            return 2
        if not isinstance(payload, dict):
            print(
                f"converge aggregate: {path} does not carry a JSON object",
                file=sys.stderr,
            )
            return 2
        runs.append(payload)

    agg = conv.aggregate_findings(runs)
    Path(args.json_out).write_text(json.dumps(agg, indent=2))
    print(
        f"aggregated {len(runs)} run(s) -> {len(agg.get('findings', []))} finding(s)",
        file=sys.stderr,
    )
    # Exit 0 even when the round is a majority failure: the aggregate is still
    # written (with error/incomplete), so the session records an INCONCLUSIVE
    # round rather than the job dying on a missing file.
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Aggregate repeated QA findings JSONs into one, by majority vote."
    )
    p.add_argument(
        "--findings",
        nargs="+",
        required=True,
        metavar="JSON",
        help="QA findings JSON files, one per repeated run (a shell glob works)",
    )
    p.add_argument(
        "--json-out",
        required=True,
        metavar="JSON",
        help="Write the aggregate findings JSON here",
    )
    return p


def main(argv=None) -> int:  # pragma: no cover
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""
Round builders shared by this package's tests.

NOT a test module -- pytest runs with --import-mode=importlib, under which test
files are not importable from each other by name. A plain module beside them is,
once its directory is on sys.path, and duplicating these builders across two
files would let the two drift into testing different shapes.

The artefacts built here are the real ones: a findings JSON in the QA schema's
shape and an agent-fix-result.json in the fix harness's. Anything else would
test the convergence layer against a fiction.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import convergence as conv  # noqa: E402

PRICES = {
    "models": {"test-model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}},
    "compute": {"vcpu_hour_usd": 0.0895, "gb_hour_usd": 0.00945},
    "source": "test",
}


def finding(page, severity, summary, fid="F-001"):
    return {
        "id": fid,
        "severity": severity,
        "page": page,
        "summary": summary,
        "evidence": "observed",
        "steps_to_reproduce": ["load the page"],
        "expected": "a number",
        "actual": "blank",
    }


def qa(findings=(), *, error=None, incomplete=False, stop_reason=None, tokens=0, seconds=0):
    """A QA findings JSON, as agents/qa/harness/main.py writes it."""
    payload = {
        "findings": list(findings),
        "session_seconds": seconds,
        "cost": {
            "model_tokens": {
                "input": tokens,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            "turns": 1,
            "model": "test-model",
        },
    }
    if error:
        payload["error"] = error
    if incomplete:
        payload["incomplete"] = True
        payload["stop_reason"] = stop_reason or "token budget"
    return payload


def fix(applied=(), *, skipped=(), calls=1, spent=1000, budget=True):
    """
    An agent-fix-result.json. `budget=False` reproduces a result written by a
    fix harness from before the budget block existed -- the case the cumulative
    token check has to refuse rather than treat as zero.
    """
    result = {
        "applied": [
            {"finding_id": fid, "path": "frontend/src/x.tsx", "lines": 3, "rationale": "r"}
            for fid in applied
        ],
        "skipped": [{"finding_id": fid, "reason": reason} for fid, reason in skipped],
        "excluded": [],
        "errors": [],
    }
    if budget:
        result["budget"] = {
            "model": "test-model",
            "calls": calls,
            "input_tokens": spent,
            "output_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "spent": spent,
            "token_budget": 200_000,
        }
    return result


def rnd(number, findings=(), fix_result=None, *, seconds=60, **kw):
    return conv.round_record(number, qa(findings, **kw), fix_result, seconds=seconds, prices=PRICES)


A = finding("/fleet", "HIGH", "health score renders NaN", "F-001")
B = finding("/fleet", "CRITICAL", "history tab throws", "F-002")
C = finding("/analytics", "HIGH", "fuel column is blank", "F-003")
LOW = finding("/fleet", "LOW", "tooltip is truncated", "F-004")

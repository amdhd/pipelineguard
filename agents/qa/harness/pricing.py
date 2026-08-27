"""
Turning the units the agent reports into money.

PLAN.md 1d/C3: a QA run bills on THREE meters, and reporting only tokens
understates it -- worst of all exactly when the agent is slow, since browser
memory bills on wall-clock including idle. That is the case you most want
visibility into.

THE HONESTY RULE HERE
---------------------
The agent reports UNITS -- tokens, seconds. Units are measured and are always
right. Dollars are an ESTIMATE derived from a price table, and a price table
goes stale.

So an unknown model yields `None`, never `0.0`. A silent zero is a confident
wrong number: it reads as "this run was free" when it means "nobody told me the
price". The PR comment renders `None` as "unpriced" alongside the real token
count, which is a thing a reader can act on.

Prices are NOT hardcoded from memory. See PRICING.md for where each came from
and how to refresh it. The AWS Price List API does not currently carry the
current-generation Anthropic models for ap-southeast-1 -- checked by paginating
both the `model` attribute and the regional product list -- so token prices must
be filled in from the Bedrock pricing page and dated.
"""

import json
import os
from pathlib import Path

# AgentCore compute meter. Both the runtime session and the browser session bill
# on this. Source and date live in PRICING.md.
DEFAULT_COMPUTE = {
    "vcpu_hour_usd": 0.0895,
    "gb_hour_usd": 0.00945,
}

# Assumed session shape when AgentCore does not report it. Deliberately explicit:
# an assumption in a cost figure should be visible in the code that made it.
DEFAULT_SESSION_VCPU = 1.0
DEFAULT_SESSION_GB = 2.0

_PRICES_PATH = Path(__file__).with_name("prices.json")


def load_prices(path: Path | None = None) -> dict:
    """
    Load the price table. Missing file is not an error -- the harness still
    reports units, and marks dollars unpriced.
    """
    p = path or _PRICES_PATH
    if os.environ.get("QA_PRICES_PATH"):
        p = Path(os.environ["QA_PRICES_PATH"])
    if not p.exists():
        return {"models": {}, "compute": DEFAULT_COMPUTE, "source": "defaults (no prices.json)"}
    data = json.loads(p.read_text())
    data.setdefault("models", {})
    data.setdefault("compute", DEFAULT_COMPUTE)
    return data


def model_cost_usd(model: str, input_tokens: int, output_tokens: int, prices: dict) -> float | None:
    """Dollars for model inference, or None when the model is not in the table."""
    entry = prices.get("models", {}).get(model)
    if not entry:
        return None
    return (
        input_tokens / 1_000_000 * entry["input_usd_per_mtok"]
        + output_tokens / 1_000_000 * entry["output_usd_per_mtok"]
    )


def compute_cost_usd(seconds: int, prices: dict, *, vcpu: float | None = None, gb: float | None = None) -> float:
    """
    Dollars for one AgentCore session of `seconds`.

    Memory is billed for every second the session is alive INCLUDING IDLE, which
    is why wall-clock is a first-class budget in the agent and not just a
    timeout. CPU billing is consumption-based and therefore over-estimated here;
    the result is deliberately an upper bound, since a cost report that flatters
    itself is worse than one that does not.
    """
    c = prices.get("compute", DEFAULT_COMPUTE)
    hours = seconds / 3600
    return hours * (
        (vcpu if vcpu is not None else DEFAULT_SESSION_VCPU) * c["vcpu_hour_usd"]
        + (gb if gb is not None else DEFAULT_SESSION_GB) * c["gb_hour_usd"]
    )


def summarise(findings: dict, prices: dict | None = None) -> dict:
    """
    Build the cost block for the PR comment from an agent response.

    Two sessions run concurrently for roughly the same wall-clock -- the runtime
    hosting the agent, and the browser it drives -- so both are charged against
    session_seconds rather than one of them being folded into the other.
    """
    prices = prices if prices is not None else load_prices()
    cost = findings.get("cost", {}) or {}
    tokens = cost.get("model_tokens", {}) or {}
    model = cost.get("model", "unknown")

    inp = int(tokens.get("input", 0))
    out = int(tokens.get("output", 0))
    seconds = int(findings.get("session_seconds", 0))

    model_usd = model_cost_usd(model, inp, out, prices)
    runtime_usd = compute_cost_usd(seconds, prices)
    browser_usd = compute_cost_usd(seconds, prices)

    known = [v for v in (model_usd, runtime_usd, browser_usd) if v is not None]
    total = sum(known) if len(known) == 3 else None

    return {
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "session_seconds": seconds,
        "turns": int(cost.get("turns", 0)),
        "model_usd": model_usd,
        "runtime_usd": runtime_usd,
        "browser_usd": browser_usd,
        "estimated_total_usd": total,
        "unpriced": model_usd is None,
        "price_source": prices.get("source", "unknown"),
        "excludes": cost.get("excludes", ["S3 storage", "CloudWatch Logs", "GitHub runner minutes"]),
    }


def fmt_usd(value: float | None) -> str:
    """Render dollars, or say plainly that we do not know."""
    if value is None:
        return "unpriced"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"

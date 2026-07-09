"""
Thin wrapper around the Infracost CLI (shipped via a Lambda layer at
/opt/infracost). Runs `infracost diff` against a Terraform plan JSON and
returns a normalised summary: the monthly cost delta of the change plus the
biggest cost drivers.

`diff` (not `breakdown`) is required: only `diff` populates diffTotalMonthlyCost,
which is the delta the cost gate thresholds on. `breakdown` reports the absolute
projected cost with no delta field, so the gate could never see an increase.
"""

import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger()

INFRACOST_BIN = os.environ.get("INFRACOST_BIN", "/opt/infracost")


def run_infracost(plan_path: str, api_key: str) -> dict[str, Any]:
    """
    Run Infracost against a Terraform plan JSON file.

    Returns a dict with:
      - monthly_cost_delta: float (USD/month increase vs. prior state)
      - total_monthly_cost: float
      - top_resources: list of the highest-cost changed resources
    """
    env = {**os.environ, "INFRACOST_API_KEY": api_key}

    cmd = [
        INFRACOST_BIN,
        "diff",
        "--path",
        plan_path,
        "--format",
        "json",
    ]
    logger.info("Running: %s", " ".join(cmd[:2]))

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"infracost failed (exit {proc.returncode}): {proc.stderr}")

    return _parse_diff(json.loads(proc.stdout))


def _parse_diff(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise the raw Infracost `diff` JSON into the summary we care about.

    `infracost diff` reports the plan's cost delta via diffTotalMonthlyCost and,
    per project, a `diff` block listing only the changed resources whose
    monthlyCost is that resource's contribution to the delta.
    """
    total_monthly = float(data.get("totalMonthlyCost") or 0.0)
    diff_monthly = float(data.get("diffTotalMonthlyCost") or 0.0)

    # Collect the changed resources across all projects to find the top drivers
    # of the delta. Fall back to the full breakdown if a diff block is absent.
    resources: list[dict[str, Any]] = []
    for project in data.get("projects", []):
        changed = (project.get("diff") or {}).get("resources")
        if changed is None:
            changed = (project.get("breakdown") or {}).get("resources", [])
        for res in changed:
            cost = float(res.get("monthlyCost") or 0.0)
            if cost != 0:
                resources.append({"name": res.get("name"), "monthly_cost": round(cost, 2)})

    resources.sort(key=lambda r: abs(r["monthly_cost"]), reverse=True)

    return {
        "monthly_cost_delta": round(diff_monthly, 2),
        "total_monthly_cost": round(total_monthly, 2),
        "top_resources": resources[:5],
    }

"""
Wrapper around Checkov (installed via a Lambda layer). Runs Checkov against a
directory of Terraform files and returns severity counts plus failed checks.

Checkov's community edition does not attach CVSS severities to every check, so
we map by check outcome: any failed check is treated as HIGH unless Checkov
reports an explicit severity.

Fail-closed contract: this module never returns zero findings for a scan that
did not actually happen. A missing directory, or one holding no Terraform,
raises — otherwise "nothing was scanned" is indistinguishable from "nothing was
found" and the gate would wave the change through with no IaC coverage.
"""

import json
import logging
import os
import subprocess
from collections import defaultdict
from typing import Any

logger = logging.getLogger()

CHECKOV_BIN = os.environ.get("CHECKOV_BIN", "checkov")


def _has_terraform_files(terraform_dir: str) -> bool:
    """True when the tree holds at least one file Checkov would actually parse."""
    for _root, _dirs, files in os.walk(terraform_dir):
        if any(f.endswith((".tf", ".tf.json")) for f in files):
            return True
    return False


def run_checkov(terraform_dir: str) -> dict[str, Any]:
    """
    Run Checkov against a directory of Terraform and return a dict of
    severity -> count plus a trimmed list of failed checks.

    Raises when there is nothing to scan. Returning zeros here would read as a
    clean IaC scan and pass the gate, so an absent or empty tree must surface as
    an error the handler can fail closed on.
    """
    if not os.path.isdir(terraform_dir):
        raise RuntimeError(
            f"Terraform dir {terraform_dir} not found — refusing to report a clean "
            "IaC scan that never ran."
        )
    if not _has_terraform_files(terraform_dir):
        raise RuntimeError(
            f"No .tf/.tf.json files under {terraform_dir} — the Terraform artifact is "
            "empty or failed to extract; refusing to report a clean IaC scan."
        )

    cmd = [
        CHECKOV_BIN,
        "--directory",
        terraform_dir,
        "--output",
        "json",
        "--compact",
        "--quiet",
    ]
    logger.info("Running checkov on %s", terraform_dir)

    # Checkov exits non-zero when it finds failures; that is expected, so we do
    # not raise on a non-zero exit — we parse stdout instead.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not proc.stdout.strip():
        raise RuntimeError(f"checkov produced no output: {proc.stderr}")

    return summarise_checkov(json.loads(proc.stdout))


def summarise_checkov(data: Any) -> dict[str, Any]:
    """Reduce Checkov JSON to per-severity counts and a trimmed findings list."""
    # Checkov may emit a list (multiple frameworks) or a single object.
    blocks = data if isinstance(data, list) else [data]

    counts: dict[str, int] = defaultdict(int)
    findings: list[dict[str, Any]] = []

    for block in blocks:
        results = block.get("results", {}) if isinstance(block, dict) else {}
        for check in results.get("failed_checks", []):
            severity = (check.get("severity") or "HIGH").upper()
            counts[severity] += 1
            findings.append(
                {
                    "check_id": check.get("check_id"),
                    "resource": check.get("resource"),
                    "file": check.get("file_path"),
                    "severity": severity,
                    "description": check.get("check_name"),
                }
            )

    out: dict[str, Any] = {sev: counts.get(sev, 0) for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
    out["findings"] = findings[:25]
    return out

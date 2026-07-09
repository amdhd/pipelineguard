"""
Wrapper around the Trivy CLI (shipped via a Lambda layer at /opt/trivy).
Scans a container image and returns severity counts keyed by level.
"""

import json
import logging
import os
import subprocess
from collections import defaultdict
from typing import Any

logger = logging.getLogger()

TRIVY_BIN = os.environ.get("TRIVY_BIN", "/opt/trivy")
# Trivy needs a writable cache; Lambda only allows writes under /tmp.
TRIVY_CACHE = os.environ.get("TRIVY_CACHE_DIR", "/tmp/trivy-cache")


def run_trivy(image_uri: str) -> dict[str, Any]:
    """
    Run a Trivy image scan and return a dict of severity -> count plus the
    raw vulnerability list under 'findings'.
    """
    if not image_uri:
        raise ValueError("run_trivy called with empty image_uri")

    os.makedirs(TRIVY_CACHE, exist_ok=True)
    cmd = [
        TRIVY_BIN,
        "image",
        "--quiet",
        "--format",
        "json",
        "--severity",
        "LOW,MEDIUM,HIGH,CRITICAL",
        "--cache-dir",
        TRIVY_CACHE,
        image_uri,
    ]
    logger.info("Running trivy image scan on %s", image_uri)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=480)
    if proc.returncode != 0:
        raise RuntimeError(f"trivy failed (exit {proc.returncode}): {proc.stderr}")

    return summarise_trivy(json.loads(proc.stdout))


def summarise_trivy(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce raw Trivy JSON to per-severity counts and a trimmed findings list."""
    counts: dict[str, int] = defaultdict(int)
    findings: list[dict[str, Any]] = []

    for result in data.get("Results", []):
        for vuln in result.get("Vulnerabilities") or []:
            severity = vuln.get("Severity", "UNKNOWN").upper()
            counts[severity] += 1
            if severity in ("HIGH", "CRITICAL"):
                findings.append(
                    {
                        "id": vuln.get("VulnerabilityID"),
                        "pkg": vuln.get("PkgName"),
                        "installed": vuln.get("InstalledVersion"),
                        "fixed": vuln.get("FixedVersion"),
                        "severity": severity,
                        "title": vuln.get("Title"),
                    }
                )

    out: dict[str, Any] = {sev: counts.get(sev, 0) for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
    out["findings"] = findings[:25]
    return out

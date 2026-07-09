"""
Calls the Claude API to turn raw Trivy + Checkov JSON into a readable security
gate report. Uses claude-haiku-4-5 — scan summarisation is cheap and fast, so
there is no need for a larger model here.
"""

import json
import logging

import anthropic

logger = logging.getLogger()

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You are a DevSecOps engineer reviewing CI/CD security scan results.
Produce a concise, actionable security gate report. Be direct. No preamble.
Format: markdown. Max 800 words. Group by severity. For each finding include:
what it is, why it matters, and one-line remediation.
End with a clear GO / NO-GO recommendation and a one-sentence reason."""


def summarise_findings(
    trivy_results: dict,
    checkov_results: dict,
    anthropic_api_key: str,
) -> str:
    """Return a markdown security report, falling back to raw counts on API error."""
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    user_message = f"""Security scan results from this CI/CD pipeline run:

## Trivy Container Scan Results
```json
{json.dumps(trivy_results, indent=2)}
```

## Checkov IaC Scan Results
```json
{json.dumps(checkov_results, indent=2)}
```

Produce the security gate report now."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except Exception as e:  # noqa: BLE001 — never let summarisation break the gate
        logger.error("Claude summarisation failed: %s", e)
        critical = trivy_results.get("CRITICAL", 0) + checkov_results.get("CRITICAL", 0)
        high = trivy_results.get("HIGH", 0) + checkov_results.get("HIGH", 0)
        return (
            f"## Security Gate Report (Claude unavailable)\n\n"
            f"- CRITICAL findings: {critical}\n"
            f"- HIGH findings: {high}\n\n"
            f"{'❌ NO-GO' if critical + high > 0 else '✅ GO'}"
        )

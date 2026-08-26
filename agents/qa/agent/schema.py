"""
Findings schema for the QA agent.

Prime directive 6: anything the agent emits that is not valid JSON matching this
shape is a BUG IN THE PROMPT, not a finding. Reject and log; never parse free
text into a findings table. In the reference implementation, unparsed agent
narration leaked into the findings table as HIGH-severity rows reading things
like "Let me verify this further" -- noise presented as bugs.

Validation lives here, in the agent package, so a malformed response is caught
before it is ever returned. The harness validates again on receipt; that is
deliberate belt-and-braces across a trust boundary, not redundancy.

Stdlib only -- no jsonschema dependency. The shape is small enough that hand
validation is clearer than a schema document, and it keeps the deployment zip
smaller.
"""

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
OVERALL = ("PASS", "FAIL")

# Severities that block. Used by the convergence check in Phase 3 and by the
# workflow's exit code.
BLOCKING = ("CRITICAL", "HIGH")

_FINDING_REQUIRED = (
    "id",
    "severity",
    "page",
    "summary",
    "evidence",
    "steps_to_reproduce",
    "expected",
    "actual",
)


class SchemaError(ValueError):
    """Raised when the agent's response does not match the findings schema."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise SchemaError(message)


def validate_finding(finding: object, index: int) -> None:
    """Validate one finding. Raises SchemaError with the index for locatability."""
    where = f"findings[{index}]"
    _require(isinstance(finding, dict), f"{where} is not an object")
    assert isinstance(finding, dict)  # for type checkers

    for key in _FINDING_REQUIRED:
        _require(key in finding, f"{where} is missing required key '{key}'")

    _require(
        finding["severity"] in SEVERITIES,
        f"{where}.severity is {finding['severity']!r}, expected one of {SEVERITIES}",
    )
    _require(
        isinstance(finding["steps_to_reproduce"], list)
        and all(isinstance(s, str) for s in finding["steps_to_reproduce"]),
        f"{where}.steps_to_reproduce must be a list of strings",
    )
    for key in ("id", "page", "summary", "evidence", "expected", "actual"):
        _require(isinstance(finding[key], str), f"{where}.{key} must be a string")

    # Optional, but type-checked when present. suspected_source is explicitly
    # nullable -- the agent is told to say "I don't know" rather than guess, and
    # Phase 2 uses it to seed file selection.
    if finding.get("suspected_source") is not None:
        _require(
            isinstance(finding["suspected_source"], str),
            f"{where}.suspected_source must be a string or null",
        )
    if "screenshot" in finding and finding["screenshot"] is not None:
        shot = finding["screenshot"]
        _require(isinstance(shot, dict), f"{where}.screenshot must be an object or null")
        _require("key" in shot, f"{where}.screenshot is missing 'key'")


def validate(payload: object) -> dict:
    """
    Validate a complete agent response.

    Returns the payload unchanged on success so it can be used inline.
    Raises SchemaError on any deviation -- there is no partial acceptance, because
    a response we had to repair is a response we cannot trust the rest of.
    """
    _require(isinstance(payload, dict), "response is not a JSON object")
    assert isinstance(payload, dict)

    for key in ("overall", "pages_tested", "findings"):
        _require(key in payload, f"response is missing required key '{key}'")

    _require(
        payload["overall"] in OVERALL,
        f"overall is {payload['overall']!r}, expected one of {OVERALL}",
    )
    _require(
        isinstance(payload["pages_tested"], int) and payload["pages_tested"] >= 0,
        "pages_tested must be a non-negative integer",
    )
    _require(isinstance(payload["findings"], list), "findings must be a list")

    for i, finding in enumerate(payload["findings"]):
        validate_finding(finding, i)

    # Cross-field consistency. An agent that reports PASS while listing a
    # blocking finding has contradicted itself, and silently trusting either half
    # would be worse than rejecting the run.
    blocking = [f for f in payload["findings"] if f["severity"] in BLOCKING]
    _require(
        not (payload["overall"] == "PASS" and blocking),
        f"overall is PASS but {len(blocking)} blocking finding(s) were reported",
    )

    ids = [f["id"] for f in payload["findings"]]
    _require(len(ids) == len(set(ids)), "finding ids are not unique")

    return payload


def has_blocking(payload: dict) -> bool:
    """True if any finding is CRITICAL or HIGH."""
    return any(f["severity"] in BLOCKING for f in payload.get("findings", []))


def finding_fingerprint(finding: dict) -> str:
    """
    Stable identity for a finding across rounds, for the Phase 3 convergence
    check. Deliberately excludes `id` (renumbered every run) and `evidence`
    (free text that varies between runs describing the same defect).
    """
    return f"{finding['page']}::{finding['severity']}::{finding['summary'].strip().lower()}"

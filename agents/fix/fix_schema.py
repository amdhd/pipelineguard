"""
The fix model's response schema.

Same rule as the QA agent's `schema.py`, and for the same reason: anything the
model emits that is not valid JSON matching this shape is a BUG IN THE PROMPT,
not a patch. Reject and report; never parse free text into an edit.

The stakes are higher on this side. An unparsed narration leaking into the QA
findings table produced a noisy PR comment. An unparsed narration leaking into
this table would produce a WRITE. So there is no partial acceptance and no
repair step -- a response we had to fix up is a response we cannot trust the
rest of.

NAMED fix_schema.py, NOT schema.py. `agents/qa/agent/schema.py` is already on
sys.path in the test session, and a second `schema` module would resolve to
whichever pytest imported first. The QA harness documents the same collision for
`handler.py`; this package hit it once already with `select.py`.
"""

_EDIT_REQUIRED = ("finding_id", "file", "old_string", "new_string", "rationale")
_SKIP_REQUIRED = ("finding_id", "reason")


class FixSchemaError(ValueError):
    """Raised when the model's response does not match the shape below."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise FixSchemaError(message)


def validate(payload: object) -> dict:
    """Validate a complete response. Returns it unchanged so it can be used inline."""
    _require(isinstance(payload, dict), "response is not a JSON object")
    assert isinstance(payload, dict)

    for key in ("edits", "skipped"):
        _require(key in payload, f"response is missing required key '{key}'")
        _require(isinstance(payload[key], list), f"{key} must be a list")

    for i, edit in enumerate(payload["edits"]):
        where = f"edits[{i}]"
        _require(isinstance(edit, dict), f"{where} is not an object")
        for key in _EDIT_REQUIRED:
            _require(key in edit, f"{where} is missing '{key}'")
            _require(isinstance(edit[key], str), f"{where}.{key} must be a string")
        # Checked here as well as in edits.py. This one catches a PROMPT
        # failure -- the model was told an empty old_string is not an insertion
        # point -- while the check in edits.py protects the filesystem. Same
        # condition, two different things going wrong.
        _require(edit["old_string"] != "", f"{where}.old_string is empty")

    for i, skip in enumerate(payload["skipped"]):
        where = f"skipped[{i}]"
        _require(isinstance(skip, dict), f"{where} is not an object")
        for key in _SKIP_REQUIRED:
            _require(key in skip, f"{where} is missing '{key}'")
            _require(isinstance(skip[key], str), f"{where}.{key} must be a string")

    return payload

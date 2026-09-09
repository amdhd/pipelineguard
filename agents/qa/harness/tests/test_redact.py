"""
Redaction tests.

The harness publishes two durable records on a PUBLIC repo -- the `--json-out`
artifact and the PR comment -- and a presigned URL in either is a real key id
and session token in a permanent, world-readable, machine-readable place. That
is the artifact class that caused PR #77.

NO VALUE BELOW IS KEY-SHAPED. That took three tries to get right upstream: the
first fixture reused the real key id from the leak (caught by the gitleaks
gate), the second used AWS's own published documentation example and GitHub's
scanner alerted on it within minutes. These assert on parameter NAMES and never
look at the values, so the values only have to make the string read as a URL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redact  # noqa: E402

# SigV2 shape: the older `AWSAccessKeyId=` presigned URL.
SIGV2 = (
    "https://b.s3.amazonaws.com/screenshots/run-7/a.png"
    "?AWSAccessKeyId=EXAMPLE-KEY-ID&Signature=EXAMPLE-SIGNATURE%3D"
    "&x-amz-security-token=EXAMPLE-SESSION-TOKEN&Expires=1788494798"
)
# SigV4 shape: what boto3 actually emits today.
SIGV4 = (
    "https://b.s3.ap-southeast-1.amazonaws.com/screenshots/run-7/a.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=EXAMPLE-KEY-ID%2F20260909%2Fap-southeast-1%2Fs3%2Faws4_request"
    "&X-Amz-Security-Token=EXAMPLE-SESSION-TOKEN&X-Amz-Signature=EXAMPLE-SIGNATURE"
)


class TestPresignedUrlsAreRemoved:
    def test_the_screenshot_url_is_dropped_and_the_key_survives(self):
        """The key is what identifies the evidence and what a reader presigns
        again later; dropping it too would make the record useless."""
        out = redact.without_credentials(
            {"screenshots": [{"key": "screenshots/run-7/a.png", "label": "a", "url": SIGV2}]}
        )
        shot = out["screenshots"][0]
        assert "url" not in shot
        assert shot == {"key": "screenshots/run-7/a.png", "label": "a"}

    def test_a_per_finding_screenshot_url_goes_too(self):
        out = redact.without_credentials(
            {"findings": [{"id": "F-001", "screenshot": {"key": "k.png", "url": SIGV4}}]}
        )
        assert out["findings"][0]["screenshot"] == {"key": "k.png"}
        assert out["findings"][0]["id"] == "F-001"

    def test_a_url_under_an_unanticipated_field_goes_too(self):
        """The whole reason this is content-based. The agent's strip knows two
        field names; this layer must not need to know any."""
        out = redact.without_credentials(
            {"findings": [{"id": "F-001", "evidence_href": SIGV4, "nested": {"deep": {"u": SIGV2}}}]}
        )
        assert "evidence_href" not in out["findings"][0]
        assert out["findings"][0]["nested"]["deep"] == {}

    def test_a_url_in_a_list_of_strings_is_redacted_in_place(self):
        """Dropping a list element would renumber the rest, so the second pass
        replaces the value instead of removing it -- shape preserved."""
        out = redact.without_credentials({"notes": ["fine", SIGV4, "also fine"]})
        assert out["notes"] == ["fine", "[redacted credential]", "also fine"]

    def test_the_url_KEY_alone_is_not_what_triggers_it(self):
        """A `url` field that is not a credential -- the target under test, a
        docs link -- has to survive, or the comment loses ordinary links."""
        out = redact.without_credentials({"target": {"url": "https://example.com/login"}})
        assert out["target"]["url"] == "https://example.com/login"

    def test_a_clean_report_is_returned_unchanged(self):
        clean = {
            "overall": "FAIL",
            "findings": [{"id": "F-001", "summary": "Signature pad does not clear"}],
            "screenshots": [{"key": "screenshots/run-7/a.png"}],
        }
        assert redact.without_credentials(clean) == clean

    def test_the_callers_dict_is_not_mutated(self):
        """Redaction happens on the way OUT. A caller that still holds the
        original must keep it -- silently emptying it would be a worse bug than
        the one this module exists to prevent."""
        findings = {"screenshots": [{"key": "k.png", "url": SIGV2}]}
        redact.without_credentials(findings)
        assert findings["screenshots"][0]["url"] == SIGV2

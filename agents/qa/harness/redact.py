"""
Credential redaction for everything the harness PUBLISHES.

The agent already keeps presigned URLs out of the S3 archive (see
``_without_presigned_urls`` in ``agents/qa/agent/agent.py``): a durable object
must not carry credentials. The dict the harness RECEIVES still has them --
the comment renderer used them for inline evidence -- and the harness has two
durable sinks of its own:

  * ``--json-out``, which the target workflow uploads as a build artifact. On a
    PUBLIC repo that artifact is downloadable by anyone. Measured 2026-09-09: a
    live, unexpired vesselAI artifact carried 12 ``AWSAccessKeyId`` and 12
    ``x-amz-security-token`` values.
  * ``--comment-out``, posted as a PR comment. Also public, also permanent.

Neither grants live access for long. A presigned URL authorizes exactly one GET
on one key, never carries the secret access key, and dies with the runtime's
STS session (~1h) long before the 7-day bucket lifecycle. But a real key id in
a public, permanent, machine-readable record is the artifact class that caused
PR #77, and it is what GitHub's secret scanner alerts on.

Content-based rather than field-name-based, deliberately. The agent's strip is
coupled to two field names; this is the layer that does not have to know where
a URL lives.
"""

import json
import re

# The query parameters that make a URL a credential. SigV2 carries
# AWSAccessKeyId and SigV4 carries X-Amz-Credential, so matching the key id
# catches every presigned shape boto3 can emit. One alternation feeds both
# patterns below so the two cannot drift apart.
_PARAMS = r"X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|AWSAccessKeyId"
_MARKERS = re.compile(_PARAMS, re.IGNORECASE)
_CREDENTIALED_STRING = re.compile(rf'"[^"]*(?:{_PARAMS})[^"]*"', re.IGNORECASE)


def _drop(node):
    """Every dict entry whose value is a credential-bearing string, removed."""
    if isinstance(node, dict):
        return {
            k: _drop(v)
            for k, v in node.items()
            if not (isinstance(v, str) and _MARKERS.search(v))
        }
    if isinstance(node, list):
        return [_drop(v) for v in node]
    return node


def without_credentials(findings: dict) -> dict:
    """
    A copy with every presigned URL gone.

    Two passes, because they fail differently. The walk DROPS the whole entry,
    which is what the comment renderer needs: a ``url`` replaced by a
    placeholder is still truthy, so the renderer would emit
    ``![evidence](<placeholder>)`` and produce a broken image instead of the
    key-only fallback. The serialized pass then catches what the walk cannot
    drop without changing shape -- a URL sitting in a list of strings, say.

    Returns a new object; the caller's dict is untouched.
    """
    walked = _drop(findings)
    return json.loads(
        _CREDENTIALED_STRING.sub('"[redacted credential]"', json.dumps(walked))
    )

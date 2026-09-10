"""The harness declares its dependency closure, and CI audits it (P0.6).

WHY: this was the only Python component in the repo that declared nothing --
`agents/fix`, `agents/converge` and both gates all ship a requirements.txt, and
`agents/converge`'s is deliberately EMPTY with a comment saying so. The QA
harness had no file at all, so `pip-audit` had nothing to point at and its
closure was the one nothing could see. At the time the file was added, the
resolved environment carried urllib3 1.26.20 -- five open advisories, no fix on
that branch -- while the agent lock beside it pinned 2.7.0 and audited clean.

These tests guard the three ways that gap can silently reopen:

  1. an entry is unpinned or the file is emptied, so `pip-audit` reasons about
     whatever resolves that day rather than a fixed closure;
  2. urllib3 drifts back below the line that carries the advisories, which is
     the specific failure the file was written to close;
  3. CI stops auditing (or stops installing from) the file, which would make it
     documentation rather than a gate.

Stdlib-only reads of two text files -- no imports of the harness, so the CI
python-gates job can run them anywhere in the tree.
"""

import re
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]

REQUIREMENTS = HARNESS_DIR / "requirements.txt"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.\-]+)$")


def _entries():
    """{name: version} for every requirement line; comments and blanks skipped."""
    pins = {}
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN.match(line)
        assert m, f"requirement {line!r} is not pinned with a bare =="
        pins[m.group(1).lower()] = m.group(2)
    return pins


def test_the_file_exists_and_is_not_empty():
    """
    Unlike agents/converge, an empty declaration here would be WRONG rather than
    meaningful: main.py and score.py both import boto3 at module scope.
    """
    assert REQUIREMENTS.exists()
    assert _entries(), "the harness has real imports; an empty file is not the declaration"


def test_every_entry_is_pinned_exactly():
    """
    A floor is what let the agent's own closure drift a minor version into
    production unseen (websocket-client 1.9.2, see the agent's requirements.txt).
    A floor here would additionally let the audited version and the installed
    version disagree, which makes the gate below meaningless.
    """
    assert _entries()  # _entries() asserts the pin shape per line


def test_the_direct_imports_are_declared():
    """boto3 and botocore are what main.py and score.py actually import."""
    entries = _entries()
    assert "boto3" in entries
    assert "botocore" in entries


def test_urllib3_is_above_the_advisory_line():
    """
    THE SPECIFIC REGRESSION THIS FILE EXISTS TO PREVENT. urllib3 reaches the
    harness transitively through botocore, so pinning boto3 alone would leave it
    free to resolve back to the 1.26 line -- which carries PYSEC-2026-141,
    -1994, -1996, -1998 and -1999 with no fix available on that branch. 2.7.0 is
    the floor that resolves all five.
    """
    version = _entries().get("urllib3")
    assert version, "urllib3 must be pinned explicitly, not left to botocore's range"
    major, minor = (int(p) for p in version.split(".")[:2])
    assert (major, minor) >= (2, 7), (
        f"urllib3 {version} predates the fixes for the five advisories this pin closes"
    )


def test_ci_audits_the_harness_closure():
    """
    Without this step the file is documentation, not a gate -- and a gate that
    always goes green tells nobody anything, which is the standard ci.yml
    already sets for npm audit and the agent lock.
    """
    ci = CI.read_text()
    assert "pip-audit -r agents/qa/harness/requirements.txt" in ci
    assert "|| true" not in ci.split("pip-audit -r agents/qa/harness/requirements.txt")[1][:200]


def test_ci_installs_the_pinned_versions_it_audits():
    """
    An unpinned `pip install boto3` alongside a pinned audit would let the tests
    pass on a closure nothing had checked -- the exact split that hid the
    vulnerable urllib3 in the first place.
    """
    assert "pip install -r agents/qa/harness/requirements.txt" in CI.read_text()

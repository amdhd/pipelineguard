"""
Which files the fix agent may touch.

THIS MODULE IS THE CONTROL. Everything else in this package is plumbing around
it: the model proposes, and this decides whether a proposal is even eligible to
be applied. It makes no model calls, does no I/O, and imports nothing outside
the standard library, so it can be tested exhaustively and read in one sitting.

TWO LISTS, IN THIS ORDER
------------------------
1. An ALLOW-LIST, checked first and failing closed. PLAN.md Phase 2: "A
   deny-list fails open on anything nobody anticipated. Since the target is one
   React frontend, invert it." A path that is not under an allowed prefix is
   rejected without ever consulting the deny-list.
2. A DENY-LIST, as defence in depth. It exists for the paths that ARE inside the
   allow-list and still must not be written -- `frontend/src/.env.local`,
   `backend/src/config/secrets.ts`, a stray `.pem`. Those are the realistic
   escapes, and they are the only ones the deny-list can still catch once the
   allow-list has done its work.

NO GLOBS, DELIBERATELY
----------------------
The plan writes the deny-list as `**/*.tf`, `.github/**` and so on, which reads
well and does not survive contact with `fnmatch`, whose `*` crosses `/` and
whose `**` means nothing special. A control whose semantics depend on which
glob library you reached for is a control nobody can audit. So the patterns are
re-expressed below as explicit segment, basename and suffix checks that mean
exactly what they say.

TRAVERSAL IS THE FIRST CHECK, NOT THE LAST
------------------------------------------
`frontend/src/../../../../etc/passwd` starts with an allowed prefix. So does
`frontend/src/./../../infra/main.tf`. Prefix matching on a raw string is not a
path check, and this is the single most likely way an allow-list gets defeated
-- so normalisation happens before anything else, absolute paths are refused
outright, and any surviving `..` component is fatal.
"""

import posixpath

# The QA target is one React frontend plus one Node backend. Nothing else in
# that six-module monorepo is under test, so nothing else is patchable --
# `frontend-angular/` in particular is a second, separate frontend that
# DISCOVERY.md section 6 records as explicitly not under test.
ALLOWED_PREFIXES = (
    "frontend/src/",
    "backend/src/",
)

# Path SEGMENTS that are never writable, wherever they appear. These are the
# plan's `terraform/**`, `.github/**`, `buildspecs/**`, `k8s/**`, `scripts/**`,
# `gates/**` and `agents/**` -- re-expressed as "no component of the path may be
# one of these", which is what those globs were trying to say.
DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".github",
        "agents",
        "buildspecs",
        "gates",
        "infra",
        "k8s",
        "node_modules",
        "scripts",
        "terraform",
    }
)

# Suffixes that are never writable. Infrastructure, keys, certificates.
DENIED_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".tf",
    ".tfstate",
    ".tfvars",
)

# Exact basenames that are never writable.
DENIED_BASENAMES = frozenset(
    {
        "Makefile",
        "backend.conf",
        "docker-compose.prod.yml",
        "docker-compose.yml",
    }
)

# Basename FRAGMENTS that are never writable. Deliberately substring matches:
# `secrets.ts`, `my-secret.json` and `.env.local` are all the same hazard, and
# an exact-name list would catch none of them.
DENIED_BASENAME_FRAGMENTS = (
    ".env",
    "credential",
    "secret",
)

# The plan's "any IAM policy document". There is no general way to recognise one
# by content without reading it, but the realistic case inside an allow-listed
# source tree is a JSON file with "policy" in its name, and the cost of a false
# positive here is one skipped finding with a printed reason.
DENIED_POLICY_SUFFIX = ".json"
DENIED_POLICY_FRAGMENT = "policy"

# Batch thresholds. PLAN.md Phase 2 asks for "max-files-touched and
# max-lines-changed" without numbers, so these are chosen against the exit
# criterion that does have one: "a human can review it in under ten minutes."
# A one-pass agent that proposes and never loops has no way to earn a larger
# diff, and a fix agent whose PR is too big to read has failed even when every
# line of it is correct. Tune them up only with a run that shows the smaller
# number rejecting something worth keeping.
MAX_FILES_TOUCHED = 5
MAX_LINES_CHANGED = 80


def normalise(path: str) -> str | None:
    """
    Reduce a proposed path to a repo-relative POSIX path, or None if it is not
    one. None means "this is not a path I am willing to reason about" -- refuse,
    do not repair.
    """
    if not path or not isinstance(path, str):
        return None
    candidate = path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or ":" in candidate:
        # Absolute, UNC, or a Windows drive letter. None of these can be
        # repo-relative, and "make it relative" is a repair, not a check.
        return None
    normalised = posixpath.normpath(candidate)
    if normalised in (".", "..") or normalised.startswith("../"):
        return None
    if any(part == ".." for part in normalised.split("/")):
        return None
    return normalised


def reject_reason(path: str) -> str | None:
    """
    Why this path may not be written, or None if it may be.

    Returns a REASON, not a bool, because every rejection is published in the PR
    summary. "Rejected" tells a reviewer nothing; "outside the allow-list" and
    "denied path segment: scripts" tell them whether the agent misunderstood the
    task or the guardrail is mis-tuned.
    """
    normalised = normalise(path)
    if normalised is None:
        return f"not a repo-relative path: {path!r}"

    if not normalised.startswith(ALLOWED_PREFIXES):
        allowed = ", ".join(ALLOWED_PREFIXES)
        return f"outside the allow-list ({allowed}): {normalised}"

    parts = normalised.split("/")
    for part in parts[:-1]:
        if part in DENIED_SEGMENTS:
            return f"denied path segment: {part}"

    basename = parts[-1]
    lowered = basename.lower()

    if basename in DENIED_BASENAMES:
        return f"denied filename: {basename}"
    if lowered.endswith(DENIED_SUFFIXES):
        return f"denied file type: {basename}"
    for fragment in DENIED_BASENAME_FRAGMENTS:
        if fragment in lowered:
            return f"denied filename fragment {fragment!r}: {basename}"
    if lowered.endswith(DENIED_POLICY_SUFFIX) and DENIED_POLICY_FRAGMENT in lowered:
        return f"looks like a policy document: {basename}"

    return None


def is_allowed(path: str) -> bool:
    """Convenience wrapper. Prefer reject_reason where the reason is reportable."""
    return reject_reason(path) is None

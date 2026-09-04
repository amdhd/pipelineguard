"""The deployment zip is built from the hash-locked closure, not requirements.txt (P2.5).

WHY: before requirements.lock existed, packaging installed from requirements.txt
with `==` on the two direct deps but no pins on the transitive closure, so two
zips built from the same commit could carry different code (websocket-client
1.9.2 drifted into production unseen). The lock fixes that -- but only while the
wiring holds. These tests guard the three ways it can silently regress:

  1. a direct dep is bumped in requirements.txt but the lock is not regenerated
     (build would keep the old version forever, silently);
  2. someone strips the hashes or unpins an entry in the lock
     (`--require-hashes` would then refuse every future package, at build time);
  3. the packaging script is pointed back at requirements.txt (the hole reopens).

These are stdlib-only reads of two text files and one script -- no imports of the
agent, so the CI python-gates job (which installs only boto3/pytest/
websocket-client) can run them anywhere in the tree.
"""

import re
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.\-]+)")
ENTRY = re.compile(r"^[A-Za-z0-9_.-]+==")


def _direct_pins():
    """requirements.txt: the human-authored {name: version} direct deps."""
    pins = {}
    for line in (AGENT_DIR / "requirements.txt").read_text().splitlines():
        m = PIN.match(line.strip())
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def _lock_entries():
    """requirements.lock: [{name, version, block: [raw lines]}] for each pin."""
    entries, current = [], None
    for line in (AGENT_DIR / "requirements.lock").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ENTRY.match(line):
            m = PIN.match(line.strip())
            current = {"name": m.group(1), "version": m.group(2), "block": [line]}
            entries.append(current)
        elif current is not None:
            current["block"].append(line)
    return entries


def test_every_direct_dep_is_locked_at_the_same_version():
    """A dep pinned in requirements.txt must be pinned to the SAME version in the
    lock -- a bump in requirements.txt without a lock regeneration is silent
    drift, the exact failure the lock exists to prevent."""
    locked = {e["name"]: e["version"] for e in _lock_entries()}
    for name, version in _direct_pins().items():
        assert name in locked, f"{name} pinned in requirements.txt but absent from requirements.lock"
        assert locked[name] == version, (
            f"{name}: requirements.txt pins {version} but the lock pins "
            f"{locked[name]} -- regenerate requirements.lock (recipe in its header)"
        )


def test_lock_is_a_closure_not_just_the_direct_deps():
    """The whole point of the lock is that it fixes the transitive closure too.
    If it ever shrinks to only the direct pins, it is no longer doing that."""
    assert len(_lock_entries()) > 10, "requirements.lock looks like a direct-pin file, not a full closure"


def test_every_lock_entry_has_at_least_one_hash():
    """`--require-hashes` (in the package script) refuses any entry without a
    hash -- and that failure would only surface at build time. Each pinned
    entry must carry >=1 sha256 hash so a rebuild is reproducible and the
    script's --require-hashes can never reject its own input."""
    for entry in _lock_entries():
        assert any("--hash=sha256:" in raw for raw in entry["block"]), (
            f"{entry['name']}=={entry['version']} has no --hash in requirements.lock"
        )


def test_package_script_installs_from_the_lock_with_require_hashes():
    """The packaging script must consume requirements.lock with --require-hashes.
    Pointing it back at requirements.txt would silently reopen the drift hole."""
    script = (REPO_ROOT / "scripts" / "package-qa-agent.sh").read_text()
    assert "--require-hashes" in script, "package script lost --require-hashes"
    assert "requirements.lock" in script, "package script no longer installs from requirements.lock"
    assert "${SRC}/requirements.txt" not in script, (
        "package script references requirements.txt -- the lock must be the install source"
    )

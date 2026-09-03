"""
D-4 wiring tests: how a fix chain reads on a QA run (agents/qa/harness/main.py).

The three D-4 behaviours live in run()'s prelude and tail:

  * A fix-PR run (head ref `agent-fix/...`) reconciles the ORIGIN PR's last
    report, threads the fix agent's applied fingerprints as `fix_intervened`,
    and writes fix-verdict.json into the ORIGIN namespace.
  * The origin PR's own later run, once the fix has merged up into it
    (`origin.pr == own`), reconciles its own prior and -- when clean -- writes
    the closing board.json.
  * A sidecar that reaches any other branch is ignored (anti-taint), so an
    unrelated PR never reconciles somebody else's findings.

The verdict semantics themselves (verify_report / _board_rows) live in the
converge + report tests; here the concern is which prior is fetched, which
ledger file is written, and that a failed write never fails the run.
"""

import json
import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS))
sys.path.insert(0, str(_HARNESS.parent / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "converge"))

import main as harness  # noqa: E402

_CLEAN = {"overall": "PASS", "pages_tested": 8, "findings": []}

# One HIGH blocker on /voyage. Shape matters only for verify_report, which reads
# page/severity/summary.
_PRIOR_FINDING = {
    "id": "F-001",
    "severity": "HIGH",
    "page": "/voyage",
    "summary": "Voyage History tab crashes with TypeError on open",
    "evidence": "error boundary",
    "steps_to_reproduce": ["Open /voyage"],
    "expected": "history loads",
    "actual": "crash",
}
_PRIOR = {"overall": "FAIL", "pages_tested": 8, "findings": [_PRIOR_FINDING]}

# The fingerprint the fix harness would have written into the sidecar for that
# finding (schema.finding_fingerprint format, computed here to avoid importing
# the QA schema by a name that collides in some sessions).
_FP = "/voyage::HIGH::voyage history tab crashes with typeerror on open"

_SIDECAR = {
    "schema": "pipelineguard/fix-origin/v1",
    "origin": {"repo": "amdhd/vesselAI", "pr": "125"},
    "origin_findings_key": "reports/pr-125/latest/findings.json",
    "applied_fingerprints": [_FP],
}


def _args(tmp_path, *, namespace, **extra):
    argv = [
        "--runtime-arn", "arn:x",
        "--target-url", "https://t",
        "--reports-bucket", "bucket",
        "--report-namespace", namespace,
        "--comment-out", str(tmp_path / "comment.md"),
    ]
    for flag, value in extra.items():
        argv += [f"--{flag.replace('_', '-')}", value]
    return harness.build_parser().parse_args(argv)


class TestFixPRRun:
    """A QA run on an `agent-fix/` head reconciles the ORIGIN report."""

    def test_it_reconciles_the_origin_prior_and_writes_fix_verdict(self, monkeypatch, tmp_path):
        """
        The run has its own namespace (pr-126) but the sidecar says the fix is
        for origin pr-125, so the prior is fetched from pr-125, the applied
        fingerprint makes the absent /voyage blocker FIXED, and the verdict is
        written back into the ORIGIN namespace -- not this run's.
        """
        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "agent-fix/fix-125")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_CLEAN))

        fetched = []
        monkeypatch.setattr(
            harness, "fetch_prior_report",
            lambda bucket, namespace, **kw: (fetched.append(namespace), dict(_PRIOR))[1],
        )
        # No fix-verdict.json exists yet for this run to read back.
        monkeypatch.setattr(harness, "_fetch_json", lambda *a, **k: None)

        puts = []
        monkeypatch.setattr(
            harness, "_put_report_json",
            lambda bucket, key, payload, **kw: (puts.append((key, payload)), True)[1],
        )

        assert harness.run(_args(tmp_path, namespace="pr-126")) == 0
        # The prior fetched was the ORIGIN report, not this fix PR's own.
        assert fetched == ["pr-125"]
        # The comment shows the reconcile ledger (this is not the closing run).
        comment = Path(tmp_path / "comment.md").read_text()
        assert "Prior findings re-verified" in comment
        assert "Final reconciliation board" not in comment

        # Exactly one ledger write: fix-verdict.json into the origin namespace.
        assert len(puts) == 1
        key, payload = puts[0]
        assert key == "reports/pr-125/latest/fix-verdict.json"
        assert payload["schema"] == "pipelineguard/fix-verdict/v1"
        assert payload["fix_pr"] == 126
        assert payload["origin"]["pr"] == 125
        row = payload["rows"][0]
        assert row["status"] == "fixed"
        assert row["page"] == "/voyage"

    def test_it_fetches_its_own_namespace_when_the_head_is_not_a_fix_head(self, monkeypatch, tmp_path):
        """
        Anti-taint: the sidecar is present but this branch is neither the fix
        head nor the origin PR, so it is ignored entirely -- the run reconciles
        its own namespace and writes no ledger files.
        """
        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "refs/heads/main")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_CLEAN))

        fetched = []
        monkeypatch.setattr(
            harness, "fetch_prior_report",
            lambda bucket, namespace, **kw: (fetched.append(namespace), None)[1],
        )
        puts = []
        monkeypatch.setattr(
            harness, "_put_report_json",
            lambda bucket, key, payload, **kw: (puts.append((key, payload)), True)[1],
        )

        assert harness.run(_args(tmp_path, namespace="pr-200")) == 0
        assert fetched == ["pr-200"]
        assert puts == []


class TestOriginChain:
    """The origin PR's own run after the fix merged up into it."""

    def test_a_clean_origin_run_writes_the_final_board(self, monkeypatch, tmp_path):
        """
        origin.pr == own (pr-125), so the sidecar is honoured on the origin's own
        branch. The prior /voyage blocker is gone and its fingerprint is in the
        applied set, so the clean run renders a FINAL board and writes board.json
        -- and writes NO fix-verdict.json, because this is not a fix-PR run.
        """
        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "refs/heads/demo/d4-origin")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_CLEAN))
        monkeypatch.setattr(
            harness, "fetch_prior_report", lambda *a, **k: dict(_PRIOR)
        )
        # No fix-verdict.json on record; attribution comes from the merged
        # sidecar's applied set instead.
        monkeypatch.setattr(harness, "_fetch_json", lambda *a, **k: None)

        puts = []
        monkeypatch.setattr(
            harness, "_put_report_json",
            lambda bucket, key, payload, **kw: (puts.append((key, payload)), True)[1],
        )

        assert harness.run(_args(tmp_path, namespace="pr-125")) == 0
        comment = Path(tmp_path / "comment.md").read_text()
        assert "Final reconciliation board" in comment
        assert "Prior findings re-verified" not in comment

        assert len(puts) == 1
        key, payload = puts[0]
        assert key == "reports/pr-125/latest/board.json"
        assert payload["schema"] == "pipelineguard/board/v1"
        assert payload["overall"] == "PASS"
        row = payload["rows"][0]
        assert row["status"] == "fixed"
        assert row["source"] == "origin-run"  # no fix-verdict.json to attribute to
        assert row["label"] is None

    def test_a_still_blocking_origin_run_renders_no_board(self, monkeypatch, tmp_path):
        """
        The board is the CLOSING ledger: while a HIGH/CRITICAL is still present
        on the origin PR, the run shows the re-verify table (STILL FAILING) and
        writes nothing. A run that is not clean cannot close the loop.
        """
        still_failing = {
            "overall": "FAIL",
            "pages_tested": 8,
            "findings": [dict(_PRIOR_FINDING)],
        }
        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "refs/heads/demo/d4-origin")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(still_failing))
        monkeypatch.setattr(
            harness, "fetch_prior_report", lambda *a, **k: dict(_PRIOR)
        )
        monkeypatch.setattr(harness, "_fetch_json", lambda *a, **k: None)

        puts = []
        monkeypatch.setattr(
            harness, "_put_report_json",
            lambda bucket, key, payload, **kw: (puts.append((key, payload)), True)[1],
        )

        assert harness.run(_args(tmp_path, namespace="pr-125")) == 1  # still blocking
        comment = Path(tmp_path / "comment.md").read_text()
        assert "STILL FAILING" in comment
        assert "Final reconciliation board" not in comment
        assert puts == []


class TestLedgerWritesAreBestEffort:
    def test_a_failed_ledger_write_never_fails_the_run(self, monkeypatch, tmp_path):
        """
        board.json is a convenience for a later score.py pass; the comment is
        the product. A write that fails must not fail the run or change its exit
        code.
        """
        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "refs/heads/demo/d4-origin")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_CLEAN))
        monkeypatch.setattr(
            harness, "fetch_prior_report", lambda *a, **k: dict(_PRIOR)
        )
        monkeypatch.setattr(harness, "_fetch_json", lambda *a, **k: None)
        monkeypatch.setattr(harness, "_put_report_json", lambda *a, **k: False)

        assert harness.run(_args(tmp_path, namespace="pr-125")) == 0
        # The comment still landed.
        assert "Final reconciliation board" in Path(tmp_path / "comment.md").read_text()

    def test_no_bucket_means_no_put_object_and_no_write(self, monkeypatch, tmp_path):
        """
        The best-effort wrapper must not even construct an S3 client when the
        run has no reports bucket (the D-3 pin, extended to the D-4 emitters).
        """
        from botocore.exceptions import NoCredentialsError

        monkeypatch.setenv("FIX_ORIGIN", json.dumps(_SIDECAR))
        monkeypatch.setenv("GITHUB_HEAD_REF", "refs/heads/demo/d4-origin")
        monkeypatch.setattr(harness, "invoke", lambda *a, **k: dict(_CLEAN))
        monkeypatch.setattr(
            harness, "fetch_prior_report", lambda *a, **k: dict(_PRIOR)
        )

        def no_s3(*a, **k):
            raise NoCredentialsError

        args = _args(tmp_path, namespace="pr-125")
        args.reports_bucket = ""
        monkeypatch.setattr(harness.boto3, "client", no_s3)
        assert harness.run(args) == 0

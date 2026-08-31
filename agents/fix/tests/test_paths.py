"""
Allow-list tests.

PLAN.md Phase 2's exit criteria name one test explicitly: "Confirm by test that
a patch targeting a denied path is rejected rather than applied." That is
`TestDeniedPaths` below. It needs no credentials and no model, which is why it
is the part of Phase 2 that can be proven while the IAM role is still applying.

The rest of this file exists because an allow-list is only as good as the paths
nobody thought to try.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402


class TestAllowList:
    """Fail closed: anything not named is refused, without consulting anything else."""

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/src/pages/Dashboard.tsx",
            "frontend/src/components/Greeting.tsx",
            "backend/src/routes/fleet.ts",
            "backend/src/lib/fuelModel.ts",
        ],
    )
    def test_source_under_an_allowed_prefix_is_permitted(self, path):
        assert paths.reject_reason(path) is None

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/package.json",
            "frontend/vite.config.ts",
            "backend/package.json",
            "README.md",
            "docker-compose.prod.yml",
        ],
    )
    def test_files_beside_the_allowed_tree_are_refused(self, path):
        assert "outside the allow-list" in paths.reject_reason(path)

    def test_the_second_frontend_is_not_under_test_and_not_patchable(self):
        """
        DISCOVERY.md section 6: `frontend-angular/` is a separate frontend that
        is explicitly not under test. A prefix check written as a bare substring
        would let it through -- this is the case that catches that mistake.
        """
        reason = paths.reject_reason("frontend-angular/src/app/app.component.ts")
        assert reason is not None
        assert "outside the allow-list" in reason


class TestTraversal:
    """
    Prefix matching on a raw string is not a path check. Every case here starts
    with an allowed prefix and none of them is inside the allowed tree.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/src/../../infra/main.tf",
            "frontend/src/../../../../etc/passwd",
            "frontend/src/./../../scripts/destroy-dev.sh",
            "backend/src/../../.github/workflows/ui-qa-agent.yml",
        ],
    )
    def test_traversal_out_of_the_tree_is_refused(self, path):
        assert paths.reject_reason(path) is not None

    @pytest.mark.parametrize(
        "path",
        ["/etc/passwd", "/frontend/src/App.tsx", "C:/Windows/System32/drivers/etc/hosts"],
    )
    def test_absolute_paths_are_refused(self, path):
        assert "not a repo-relative path" in paths.reject_reason(path)

    def test_backslashes_are_normalised_before_checking(self):
        """A Windows-style separator must not smuggle a segment past the check."""
        assert paths.reject_reason("frontend\\src\\..\\..\\infra\\main.tf") is not None

    def test_a_traversal_that_lands_back_inside_is_still_allowed(self):
        """
        Normalisation, not paranoia: this path resolves to a legitimate file and
        there is no reason to refuse it. Asserting it keeps the traversal rule
        from quietly becoming "reject anything containing a dot".
        """
        assert paths.reject_reason("frontend/src/pages/../App.tsx") is None

    @pytest.mark.parametrize("path", ["", "   ", None, 42])
    def test_junk_is_refused_rather_than_repaired(self, path):
        assert paths.reject_reason(path) is not None


class TestDeniedPaths:
    """
    PLAN.md Phase 2 exit criterion, stated as a test.

    Everything here is INSIDE the allow-list. That is the point: the allow-list
    already refuses `infra/`, `.github/` and `scripts/`, so the deny-list's only
    remaining job is the hazard that lives in a source tree, and these are the
    cases where it is the sole control.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/src/.env",
            "frontend/src/.env.local",
            "backend/src/.env.production",
        ],
    )
    def test_env_files_inside_the_source_tree_are_refused(self, path):
        assert paths.reject_reason(path) is not None

    @pytest.mark.parametrize(
        "path",
        [
            "backend/src/config/secrets.ts",
            "backend/src/lib/secret-store.ts",
            "frontend/src/api/credentials.ts",
        ],
    )
    def test_secret_bearing_names_are_refused(self, path):
        assert paths.reject_reason(path) is not None

    @pytest.mark.parametrize(
        "path",
        [
            "backend/src/certs/server.pem",
            "backend/src/certs/server.key",
            "frontend/src/generated/main.tf",
            "backend/src/deploy/prod.tfvars",
        ],
    )
    def test_keys_certificates_and_terraform_are_refused(self, path):
        assert paths.reject_reason(path) is not None

    @pytest.mark.parametrize(
        "path",
        [
            "backend/src/scripts/seed.ts",
            "frontend/src/agents/runner.ts",
            "backend/src/k8s/deployment.ts",
            "frontend/src/node_modules/pkg/index.js",
        ],
    )
    def test_denied_segments_are_refused_wherever_they_appear(self, path):
        reason = paths.reject_reason(path)
        assert reason is not None
        assert "denied path segment" in reason

    def test_a_policy_document_is_refused(self):
        assert paths.reject_reason("backend/src/iam/bucket-policy.json") is not None

    def test_a_denied_reason_is_specific_enough_to_act_on(self):
        """
        Every rejection is published in the PR summary. "Rejected" tells a
        reviewer nothing about whether the agent misread the task or the
        guardrail is mis-tuned.
        """
        assert "scripts" in paths.reject_reason("backend/src/scripts/seed.ts")
        assert ".env" in paths.reject_reason("frontend/src/.env.local")


class TestBatchThresholds:
    def test_caps_exist_and_are_tight(self):
        """
        Tied to the exit criterion that has a number in it -- "a human can review
        it in under ten minutes" -- not to a guess about diff size. A one-pass
        agent that never loops has no way to earn a bigger diff.
        """
        assert paths.MAX_FILES_TOUCHED <= 5
        assert paths.MAX_LINES_CHANGED <= 120

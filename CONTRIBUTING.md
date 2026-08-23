# Contributing to PipelineGuard

Thanks for taking a look. This is a portfolio / demonstration repo, but changes are held to the
same bar as the pipeline it ships: **the gates never silently pass.**

## Getting set up

Local work needs no AWS account:

```bash
# App (Node 20)
npm ci --prefix app && npm test --prefix app

# Gate handlers (Python 3.12)
pip install pytest boto3
pytest

# Security scan of the image + IaC (needs docker + trivy + checkov)
./scripts/local-scan.sh
```

Deploying the real stack — remote state, the GitHub connection, teardown — is documented in
[`docs/deploy.md`](docs/deploy.md) and [`docs/runbook.md`](docs/runbook.md).

## Repository layout

| Path | What lives there |
|---|---|
| `app/` | Sample Express API (the deployed workload) + Dockerfile + tests |
| `infra/` | All Terraform: networking, ecr, ecs, pipeline, gates modules — see [`infra/README.md`](infra/README.md) |
| `gates/` | Lambda source: `cost_gate` (zip) + `security_gate` (container image) |
| `buildspecs/` | CodeBuild YAMLs, one per pipeline stage |
| `scripts/` | bootstrap · deploy-gates · apply-dev · destroy-dev · local-scan · aws-usage |
| `docs/` | architecture · deploy · runbook |

## Non-negotiables

These are enforced in review and, where possible, by the gates themselves:

- **Everything is Terraform.** No click-ops. If you changed something in the console, put it in code
  and re-apply.
- **Secrets only in Secrets Manager** — never in code, env vars, tfvars, or buildspecs.
- **Least-privilege IAM.** Each Lambda and ECS task gets its own role; document each new permission
  with a comment explaining why it's needed.
- **Default tags on every resource** (`Project`, `Environment`, `ManagedBy`, `Owner`).
- **Gates never silently pass.** A gate that can't reach its scanner must fail, not warn.
- **Immutable, SHA-tagged ECR artifacts**, bounded log retention, S3 versioning, ECS circuit breaker.
- **Typed Python handlers** in `gates/`.

## Before you open a PR

```bash
npm test --prefix app                         # app unit tests
pytest                                        # gate handler tests
terraform -chdir=infra fmt -recursive -check  # formatting
terraform -chdir=infra validate
./scripts/local-scan.sh                       # Trivy + Checkov, same tools as the gate
```

For Terraform changes, run a plan and eyeball the cost delta — the cost gate blocks anything above
**$50/month** in dev (`cost_gate_threshold` in `infra/environments/dev.tfvars`). If an increase
is intentional, raise the threshold in the same PR and say why.

Note that the security gate is **strict by design**: Checkov flags every HIGH/CRITICAL
misconfiguration in the sample infra, so out of the box the gate blocks. Don't loosen it to get a
green run — fix the finding, or baseline it explicitly in `.checkov.yaml` with a reason.

## Pull requests

- Branch off `main`; name branches `feat/…`, `fix/…`, `chore/…`, or `docs/…`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) —
  e.g. `fix(gates): thresholds on diff, not breakdown`.
- Keep PRs focused; infra and app changes are easier to review apart.
- Fill in the [PR template](.github/pull_request_template.md), including the gate checklist.
- Never commit `backend.conf`, `*.auto.tfvars`, `plan.json`, or anything else in `.gitignore`.

## Reporting issues

Open a GitHub issue with what you expected, what happened, and the relevant output
(`terraform plan` summary, CodeBuild stage log, or gate Lambda response). For anything
security-sensitive, please email the maintainer instead of filing a public issue.

## License

By contributing you agree that your contributions are licensed under the MIT License.

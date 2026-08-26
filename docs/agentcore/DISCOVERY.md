# AgentCore QA loop — Discovery

Phase 0 of [PLAN.md](PLAN.md). A map of what actually exists across both
repositories, so later phases build on real names rather than assumed ones.

**Compiled:** 2026-08-26. Everything below was read out of the repositories, the
Terraform source, and the local AWS config. **No live AWS API call succeeded** —
the `vesselmind` SSO session is expired (`aws sts get-caller-identity` returns
"Your session has expired"), and nothing here required calling AWS. Resource
*names* are therefore derived from Terraform source, which is authoritative for
what an apply will create; where a value is interpolated the `dev` expansion is
shown. Anything that needs live confirmation is listed in
[§10 Open items](#10-open-items).

---

## 1. Accounts and regions — **both repos share one account**

| | PipelineGuard | vesselAI |
|---|---|---|
| Profile | `pipelineguard` | `vesselmind` / `vesselmind-tf` |
| Region | `ap-southeast-1` | `us-east-1` |
| Account | `149751500899` | **`149751500899` — the same** |

**Evidence.** `~/.aws/config` carries
`[profile vesselmind] login_session = arn:aws:iam::149751500899:user/vessel-k8`,
and `aws sts get-caller-identity --profile pipelineguard` returns
`149751500899`. Two profiles, two regions, **one account**.

> ### This changes Phase 1a. **Do not create a second OIDC provider.**
>
> PLAN.md §1a says "vesselAI's provider is in a different account and cannot be
> reused across the boundary… confirm in Phase 0 whether the two repos share an
> account; if they do, extend the existing provider." **They do.**
>
> vesselAI's `terraform/github-oidc.tf` already declares
> `aws_iam_openid_connect_provider.github` for
> `https://token.actions.githubusercontent.com` in this account. IAM permits
> **one provider per issuer URL per account**, so a second
> `aws_iam_openid_connect_provider` for the same URL in PipelineGuard's stack
> will fail the apply with `EntityAlreadyExists`.
>
> IAM is global, so the existing provider in `us-east-1`'s stack is usable from
> `ap-southeast-1` unchanged. PipelineGuard therefore creates **only a role**,
> whose trust policy references the existing provider ARN — supplied as a
> variable or looked up with a `data "aws_iam_openid_connect_provider"` block.
> It does not manage the provider, and must not, or two states will fight over
> one resource.
>
> **Second consequence:** the earlier reasoning for keeping the QA workflow's
> AWS writes minimal gets stronger, not weaker. A role in this account is a role
> in the same account as the live PipelineGuard stack — there is no account
> boundary providing a backstop.

---

## 2. PipelineGuard — layout and state

Flat root module in `infra/` with five child modules under `infra/modules/`.

| File | Contents |
|---|---|
| `infra/main.tf` | Wires `networking`, `ecr`, `ecs`, `pipeline`, `gates` |
| `infra/variables.tf` | 13 variables; `aws_region` defaults `ap-southeast-1`, `environment` defaults `dev` |
| `infra/outputs.tf` | 9 outputs incl. `alb_dns_name`, `github_connection_arn` |
| `infra/kms.tf` | One shared CMK, passed to every module as `kms_key_arn` |
| `infra/versions.tf` | `required_version >= 1.7`; `aws ~> 5.0`, `archive ~> 2.4`; S3 backend |
| `infra/environments/dev.tfvars` | Non-secret values only, with a comment explaining why |
| `infra/backend.conf` | Backend config, git-ignored |

**State.** S3 bucket `pipelineguard-tfstate-149751500899-ap-southeast-1`, key
`pipelineguard/dev/terraform.tfstate`, region `ap-southeast-1`.

Note a **redundancy worth knowing about before the provider upgrade**:
`versions.tf` sets `use_lockfile = true` (S3-native locking) while
`backend.conf` also sets `dynamodb_table = "pipelineguard-tflock-dev"`. Both
mechanisms are configured. This is harmless today but is exactly the kind of
thing a provider major can start warning or erroring on — check it during
Phase 0.5 #2 rather than being surprised by it.

**Provider lock.** `.terraform.lock.hcl` pins `aws 5.100.0`, `archive 2.8.0`.

**Naming convention.** `pipelineguard-<thing>-<environment>` almost everywhere;
the `pipeline` module instead uses `local.name_prefix = "pipelineguard-${var.environment}"`
and suffixes it (`pipelineguard-dev-build`). Both forms exist — match whichever
module you are editing rather than imposing one.

---

## 3. PipelineGuard — real resource names (`dev` expansion)

**Compute and network** — `infra/modules/ecs/main.tf`, `networking/main.tf`

| Resource | Name |
|---|---|
| ECS cluster | `pipelineguard-dev` |
| ECS service | `pipelineguard-app-dev` |
| ALB | `pipelineguard-alb-dev` |
| Target group | `pipelineguard-tg-dev` |
| Security groups | `pipelineguard-alb-dev`, `pipelineguard-ecs-dev` |
| ECS roles | `pipelineguard-ecs-execution-dev`, `pipelineguard-ecs-task-dev` |
| Flow-log role | `pipelineguard-flowlog-dev` |

ALB listener is **HTTP :80 only** — no ACM cert, no HTTPS
(`checkov:skip=CKV_AWS_2` with that reason inline). Container health check is
`wget -q -O- http://localhost:3000/health`.

**Registries** — `infra/modules/ecr/main.tf`, `gates/main.tf`

| Repository | Mutability | Why |
|---|---|---|
| `pipelineguard-app-dev` | Immutable, SHA-tagged | Pipeline artifact per run |
| `pipelineguard-security-gate-dev` | **Mutable**, `:latest` | Re-pushed on redeploy |

**Gates** — `infra/modules/gates/main.tf`

| Resource | Name |
|---|---|
| Cost gate Lambda | `pipelineguard-cost-gate-dev` (zip + layer) |
| Security gate Lambda | `pipelineguard-security-gate-dev` (**container image**) |
| Infracost layer | `pipelineguard-infracost-dev` |
| Shared execution role | `pipelineguard-gate-lambda-dev` |
| Secret | `pipelineguard/gates/dev` |
| Alerts topic | in `gates`, DLQ target for both Lambdas |

The security gate is a container image because Trivy + Checkov together
(~320 MB unzipped) exceed Lambda's 250 MB zip limit. Built out-of-band by
`scripts/deploy-gates.sh`.

**Pipeline** — `infra/modules/pipeline/main.tf`

| Resource | Name |
|---|---|
| CodePipeline | `pipelineguard-dev` |
| CodeBuild projects | `pipelineguard-dev-{test,build,tf-plan,cost-gate,security-gate,deploy}` |
| Roles | `pipelineguard-dev-codebuild`, `pipelineguard-dev-pipeline` |
| Artifact bucket | `pipelineguard-dev-artifacts-149751500899` |
| CodeStar connection | `pg-dev` |
| SSM parameter | `/pipelineguard/ecr_repo_url` |

**Pipeline stages, in order:** `Source` → `BuildAndTest` (Test ∥ Build) →
`TerraformPlan` → `CostGate` → `SecurityGate` → `Approval` *(conditional on
`enable_manual_approval`, default `false`)* → `Deploy`.

**Log groups:** `/ecs/pipelineguard-dev`, `/vpc/flow/pipelineguard-dev`,
`/codebuild/pipelineguard-dev`, `/aws/lambda/pipelineguard-cost-gate-dev`,
`/aws/lambda/pipelineguard-security-gate-dev`. Retention 7 days
(`log_retention_days`).

---

## 4. Orchestration, CI, and Slack

**Two orchestrators, different jobs.** CodePipeline runs in AWS post-merge and
owns the real gates. `.github/workflows/ci.yml` is a pre-merge mirror with four
jobs — `terraform` (fmt · validate · tflint), `checkov`, `python-gates`
(pytest), `node-app` (build · test · `npm audit`).

**`ci.yml` is deliberately AWS-free.** Its header states: *"Nothing here touches
AWS — no credentials, no backend, no apply."* `permissions: contents: read`.
Terraform is validated with `-backend=false`. Adding an AWS-assuming workflow is
a **posture change against a written decision**, not a gap being filled — say so
when it happens.

**Slack.** No webhook in any workflow or `.tf` file. It lives in Secrets Manager
under key `SLACK_WEBHOOK_URL` in `pipelineguard/gates/dev`, read at runtime by
`gates/security_gate/handler.py:102` and `gates/cost_gate/handler.py:74`. It is
**not** a GitHub Actions secret, so a workflow cannot reach it without either a
new IAM statement or a duplicated copy.

**No GitHub OIDC provider in PipelineGuard.** Confirmed by grep across `*.tf`,
`*.yml`, `*.sh` — zero hits for `oidc`/`OIDC`/`openid`. But see §1: one already
exists **in this account**, declared by vesselAI.

**Secrets are never Terraform variables.** `infra/modules/gates/main.tf` owns an
empty secret container; values are seeded by `scripts/seed-gate-secrets.sh`. The
reason is written inline: `terraform show -json` does not redact sensitive
values, so a `TF_VAR_*` secret lands in plaintext in `plan.json`, which ships to
S3 as a pipeline artifact. **The QA agent's secret must follow this pattern.**

---

## 5. Directly reusable components

| Asset | Path | Reuse |
|---|---|---|
| PR commenting | `gates/security_gate/github_commenter.py` | `post_pr_comment()` as-is; stdlib only |
| Commit → PR mapping | same file, `resolve_pr_number()` | Only needed for post-merge triggers |
| Slack notify | `gates/*/handler.py`, `_notify_slack()` | Same shape for QA notifications |
| Image build pattern | `scripts/deploy-gates.sh` | Template for `deploy-qa-agent.sh` (see §9) |
| ECR module | `infra/modules/ecr/` | Reuse for the agent image repo |
| Secret seeding | `scripts/seed-gate-secrets.sh` | Template for QA credentials |

**pytest wiring.** `pytest.ini` uses `--import-mode=importlib` (both gate test
files are named `test_handler.py`) and lists `testpaths` explicitly. A new
harness test directory **must be added to `testpaths`** or CI will not run it —
`ci.yml` calls a bare `pytest -q`.

---

## 6. vesselAI — the QA target

`amdhd/vesselAI`, public, TypeScript. Six-module maritime fleet platform.

| Concern | Detail |
|---|---|
| Frontend under test | `frontend/` — React 18 + Vite + Tailwind + Recharts + Leaflet |
| **Not** under test | `frontend-angular/` — a second, separate frontend |
| Backend | `backend/` — Express + Prisma, port 3001 |
| Analytics | `data-platform/` — FastAPI over DuckDB, port 8000 |
| Auth | JWT (HS256), shared `JWT_SECRET` between Express and FastAPI |
| Tests | Vitest — frontend unit + Testing Library, backend unit + supertest |
| CI | `.github/workflows/ci.yml` — `test` and `build` jobs, `actions/checkout@v7` |
| Terraform | `terraform/`, `aws ~> 5.60`, **local state by design**, domain `ahmadhadi.org` |
| Existing OIDC | `terraform/github-oidc.tf`, `StringEquals` on `repo:amdhd/vesselAI:ref:refs/heads/main` |

**Not currently deployed.** No DNS resolves for `ahmadhadi.org`,
`www`, `app`, `api`, or `vesselmind` subdomains. The README's "live demo" is
down along with the cluster.

**Demo credentials are published in the public README:**
`demo@petronas.com` / `demo123`. Storing them in Secrets Manager is the right
*pattern* to demonstrate and is worth doing, but it protects nothing that is not
already public. Do not overclaim it.

**`docker-compose.prod.yml` is the QA target** (see PLAN.md "Target
environment"). Services: `postgres` (16-alpine, healthcheck) → `migrate`
(one-off) → `seed` (one-off) → `backend`, `analytics-api`, `frontend`. Only
`frontend` publishes a port — `${WEB_PORT:-8080}:8080`.

`frontend/nginx.conf` gives a single origin: SPA at `/`, `proxy_pass` to
`backend:3001` for `/api/`, `analytics-api:8000` for `/api/analytics/` (longest
prefix wins), and `backend:3001` for `/socket.io/` with `proxy_buffering off`
for SSE.

**Required env with no defaults:** `POSTGRES_PASSWORD`, `JWT_SECRET` (≥32
chars), `ANTHROPIC_API_KEY`. The stack will not start without all three.

---

## 7. Route allow-list (from `frontend/src/App.tsx`)

Authoritative, read from the router. **Eight authenticated routes** behind
`PrivateRoute`, plus two public:

| Route | Component | Auth |
|---|---|---|
| `/login` | `LoginPage` | Public |
| `/register` | `RegisterPage` | Public |
| `/` | `DashboardPage` | Private |
| `/voyage` | `VoyagePage` | Private |
| `/maintenance` | `MaintenancePage` | Private |
| `/compliance` | `CompliancePage` | Private |
| `/ports` | `PortsPage` | Private |
| `/knowledge` | `KnowledgePage` | Private |
| `/sire` | `SirePage` | Private |
| `/analytics` | `AnalyticsPage` | Private |

`*` redirects to `/`. **A 404 is therefore not observable** — an agent probing a
bad path lands on the dashboard, and must not report that as a routing bug. Put
this in the rubric explicitly.

**Backend routes** relevant to the health gate:
`POST /api/auth/login`, `GET /api/fleet` (authenticated, Postgres-backed — also
the load-test target in vesselAI's own README), and `/health` on the analytics
service, which stays open.

---

## 8. By-design behaviours — rubric input

Lifted from vesselAI's README "What's Real vs. Mocked". **Every row in the
second group is a guaranteed false positive unless the rubric names it.**

**Real — findings here are genuine:** auth/JWT/RBAC/tenant isolation; fleet and
vessel data (Postgres via Prisma); the fuel model
(`backend/src/lib/fuelModel.ts`); Claude integration across all six modules;
the voyage agent tool-use loop; marine weather (live Open-Meteo); AIS positions
(live aisstream.io); Fleet Analytics (DuckDB medallion); bunker CSV import.

**Fixture data — do not report as broken:** equipment sensor telemetry, SIRE
findings, port congestion, voyage history. These carry deliberate statistical
variation, trends, noise, and **seeded anomalies** — an agent looking for
outliers will find them, and they are the point, not a defect.

**Two machine-readable signals the rubric should use rather than guess at:**

1. **`X-AI-Fallback`** — a header/field flagging that an AI response came from
   the canned fallback rather than a live call. Under the dummy-key default
   (PLAN.md Phase 0.5 #4) *every* AI surface sets this. The rubric can therefore
   instruct: check the flag; a flagged response is expected in fallback mode and
   is never a finding.
2. **In-memory DB fallback** — fleet data degrades to an in-memory source if
   Postgres is unreachable rather than 500ing. Under compose with a healthy
   `postgres` service this should never trigger; if the agent sees evidence of
   it, that *is* a finding, and a `CRITICAL` one.

**Known-bug corpus for false-negative measurement.** The README's "contract
audit" paragraph documents contract-drift bugs found by hand: reshaped API
responses (arrays wrapped in objects), `camelCase` vs different field names,
nested vs flat, unit mismatches (uppercase `'CRITICAL'` congestion enum on one
side, lowercase `'congested'` on the other), some 404ing but most silently
rendering `NaN` or blank charts, with no error boundary. Commit range
`a7fc00b` → `5b62621` is cited as the before/after. **Reintroduce two or three
of these on a branch as the seeded corpus** (PLAN.md Phase 1e exit criteria).

---

## 9. Decisions recorded

| Decision | Choice | Basis |
|---|---|---|
| QA target | vesselAI `frontend/` (React) | §6, §7 — real routes, real known-bug corpus |
| Target hosting | `docker-compose.prod.yml` in the runner + tunnel | Single origin, self-seeding, nothing to strand |
| EKS QA | Manual and occasional, never in CI | Fidelity matched to trigger frequency |
| Workflow location | vesselAI | PR comments and fix PRs land natively |
| AgentCore location | PipelineGuard, `ap-southeast-1` | Reusable agent infra is this repo's contribution |
| **IaC tool** | **Terraform** | See below |
| **OIDC provider** | **Reuse vesselAI's existing one** | §1 — same account, one per issuer |

**Terraform vs CDK — decided: Terraform.** `aws_bedrockagentcore_agent_runtime`
and `..._endpoint` exist in the AWS provider (AWSCC has
`awscc_bedrockagentcore_runtime` as an alternative), so the starter-toolkit
fallback is unnecessary. The real constraint is the **version**: the resource
landed in **6.18.0** and this repo pins `aws ~> 5.0` at `5.100.0`. Hence the
provider upgrade in PLAN.md Phase 0.5 #2, pinned `>= 6.18, < 7.0` rather than
`~> 6.0` — a bare `~> 6.0` resolves 6.0.0 and then fails on an unknown resource
type.

**Region availability — verified.** AgentCore Browser is available in
`ap-southeast-1`. No cross-region hop needed.

**Known provider issues to carry into the runbook:**
- [#45290](https://github.com/hashicorp/terraform-provider-aws/issues/45290) —
  `lifecycle_configuration` with only `max_lifetime` set produces "inconsistent
  result after apply". Set both fields.
- Reports of `aws_bedrockagentcore_agent_runtime` leaving undeletable ENIs and
  hanging `destroy` on a dependency cycle. This project destroys and rebuilds
  routinely, so this matters more here than it would elsewhere.

**ARM64.** AgentCore Runtime is ARM64-only and an amd64 image is rejected
*silently* — import errors at invoke time, not a clear failure at deploy. This
repo has met the sibling of this bug already: `scripts/deploy-gates.sh` uses
`docker buildx --provenance=false` because plain buildx produces an OCI index
Lambda rejects. Same tool, same silent-rejection class.

---

## 10. Open items

Things that could not be settled from source alone.

1. **Bedrock model access in `149751500899` / `ap-southeast-1`** — not
   verifiable without a live call. There is **zero Bedrock usage in this repo
   today**; `gates/security_gate/claude_summariser.py:29` calls
   `anthropic.Anthropic(api_key=...)` directly. Model access must be requested
   and the **model ARN recorded**, because the IAM policy scopes to it.
   *(PLAN.md Phase 0.5 #3.)*
2. **The existing OIDC provider's ARN** — needs a live read (or vesselAI's
   Terraform output) before PipelineGuard's role can reference it. §1.
3. **Whether `vessel-k8` vs the `pipelineguard` principal differ in
   permissions** — both are in one account, but they are different IAM
   identities and the QA role will be created by the `pipelineguard` principal.
4. **`use_lockfile` + `dynamodb_table` interaction** under AWS provider v6 — §2.
5. **`job_workflow_ref` claim value for `pull_request` runs** — PLAN.md §1a
   requires verifying the actual claim before pinning, since PR runs present a
   merge ref rather than `main`.
6. **Branch protection** — absent on both repos as of this writing
   (`gh api .../branches/main/protection` → 404, "Branch not protected"). Both
   repos are **public**.

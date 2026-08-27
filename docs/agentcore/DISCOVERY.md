# AgentCore QA loop — Discovery

Phase 0 of [PLAN.md](PLAN.md). A map of what actually exists across both
repositories, so later phases build on real names rather than assumed ones.

**Compiled:** 2026-08-26, and amended as Phase 0.5 executed.

**Evidence basis.** §§2–9 were read out of the repositories and Terraform source
rather than a live account, so resource *names* there are what an apply **will**
create; where a value is interpolated, the `dev` expansion is shown. §§1, 11 and
parts of §2 are backed by live calls under the `pipelineguard` profile, which
works. The `vesselmind` profile's SSO session is **expired**, so nothing was
verified against vesselAI's side of the account.

Remaining unknowns are in [§10 Open items](#10-open-items); Bedrock detail is in
[§11](#11-bedrock--models-and-the-arns-iam-must-scope-to).

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
| `infra/versions.tf` | `required_version >= 1.7`; `aws >= 6.18, < 7.0`, `archive ~> 2.4`; S3 backend |
| `infra/environments/dev.tfvars` | Non-secret values only, with a comment explaining why |
| `infra/backend.conf` | Backend config, git-ignored |

**State.** S3 bucket `pipelineguard-tfstate-149751500899-ap-southeast-1`, key
`pipelineguard/dev/terraform.tfstate`, region `ap-southeast-1`.

**Locking — resolved during Phase 0.5 #2.** This section originally flagged a
redundancy: `versions.tf` set `use_lockfile = true` while `backend.conf` also set
`dynamodb_table`, so both mechanisms were configured to protect one state.
Terraform already deprecated the parameter on v5, and v6 did not escalate it.

It is now **S3-native locking only**. The DynamoDB table is gone from
`scripts/bootstrap.sh`, both buildspecs, and the docs.

Investigating it surfaced a **latent bug that had not yet fired**: S3 locking
releases by *deleting* the `<key>.tflock` object, and the CodeBuild role's
`TerraformStateBackend` statement granted only `GetObject`/`PutObject`/
`ListBucket`. The first CI run under S3 locking would have taken a lock it could
never release, and every run after it would have failed to acquire one.
`use_lockfile` was added in `8035a4e` — the most recent commit, *after* the last
green pipeline run — so this path had never executed. A `TerraformStateLock`
statement now grants `s3:DeleteObject` scoped to the single lock object.

The lock object name was verified empirically rather than assumed: it is
`pipelineguard/dev/terraform.tfstate.tflock`, caught in the bucket mid-plan and
observed to be deleted on release.

**The `pipelineguard-tflock-dev` table has been deleted** (2026-08-26, on
explicit instruction). Its only content was the backend's state-digest record
(`...terraform.tfstate-md5`) — not a held lock, so nothing was stuck. There are
now **no DynamoDB tables in `ap-southeast-1`**. Note that the old `bootstrap.sh`
in git history recreates the table if run, so a checkout of an earlier commit
still works.

**Provider lock.** `.terraform.lock.hcl` pins `aws 6.61.0`, `archive 2.8.0` —
upgraded from `5.100.0` during Phase 0.5 #2.

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

1. ~~**Bedrock model access**~~ — **resolved.** The Anthropic use-case form was
   submitted and approved; both rungs invoke successfully (`OK`, 16 tokens).
   The failure mode is documented in §11 because the diagnosis is non-obvious:
   `get-foundation-model-availability` reported `AUTHORIZED`/`AVAILABLE`
   throughout, including while every call was being rejected. It is not a
   permission check.

   Still true, and still worth saying in an interview: this repo had **zero
   Bedrock usage** before the QA agent.
   `gates/security_gate/claude_summariser.py:29` calls
   `anthropic.Anthropic(api_key=...)` directly against the Anthropic API with a
   key from Secrets Manager. The two AI paths in this project now use different
   providers for different reasons — the gate wants a plain API call, the QA
   agent needs a hosted agent runtime — and that is a deliberate split, not
   drift.
2. **The existing OIDC provider's ARN** — needs a live read (or vesselAI's
   Terraform output) before PipelineGuard's role can reference it. §1.
3. **Whether `vessel-k8` vs the `pipelineguard` principal differ in
   permissions** — both are in one account, but they are different IAM
   identities and the QA role will be created by the `pipelineguard` principal.
4. ~~**`use_lockfile` + `dynamodb_table` interaction** under AWS provider v6~~ —
   **resolved.** Now S3-native locking only; see §2. The one thing still
   unverified is that it works *from the CodeBuild role* specifically — the
   release was confirmed under the local `terraform-pipelineguard` principal,
   and the new IAM statement is a plan-time construct until the stack is
   applied and a pipeline runs.
5. **`job_workflow_ref` claim value for `pull_request` runs** — PLAN.md §1a
   requires verifying the actual claim before pinning, since PR runs present a
   merge ref rather than `main`.
6. ~~**Branch protection** — absent on both repos~~ — **done.** Both `main`
   branches are now protected: PR required, force-push and deletion blocked,
   and all PR-reporting CI checks required (4 on PipelineGuard, 11 on vesselAI).

   Two settings chosen deliberately, both trivially reversible:
   - **`required_approving_review_count: 0`.** GitHub does not let you approve
     your own PR, so requiring 1 approval on a solo repo would make every
     human-authored PR unmergeable. Zero still forces the PR; the human still
     reviews and clicks merge. Raise it to 1 if a second person ever joins —
     agent-authored PRs would satisfy it, human-authored ones would not.
   - **`enforce_admins: false`.** Keeps a human escape hatch if CI wedges. It
     does *not* weaken the control that matters here: the fix agent
     authenticates with a fine-grained PAT or App installation token scoped to
     `contents` + `pull-requests`, which is **not** admin, so protection binds
     the agent regardless.

   **All 11 vesselAI checks were verified to run on `pull_request` before being
   required** — the `if: github.event_name == 'push'` guards in its `ci.yml` are
   at *step* level, not job level, so the image jobs still build and scan on a
   PR and skip only the publish. Confirmed against real PR #91. Requiring a
   check that never reports on PRs would block every merge permanently, so this
   is worth re-checking whenever that workflow changes.

---

## 11. Bedrock — models and the ARNs IAM must scope to

Verified live on 2026-08-26 from the `pipelineguard` profile, `ap-southeast-1`.

> ### RESOLVED — but the reasoning below is the part worth keeping. **[2026-08-26]**
>
> **Model invocation now works.** The Anthropic use-case form was submitted and
> approval landed in about a minute; both rungs return `OK` / 16 tokens. The
> account history, kept because it explains the failure mode:
>
> An earlier revision of this section said "access is already enabled." That was
> written from two `converse` calls that genuinely succeeded (Haiku and Sonnet,
> 16 tokens each, ~15:55). **Re-running the identical calls at ~16:10 fails:**
>
> ```
> ResourceNotFoundException: Model use case details have not been submitted for
> this account. Fill out the Anthropic use case details form before using the
> model.
> ```
>
> and `get-use-case-for-model-access` confirms it: *"You have not filled out the
> request form."*
>
> **The lesson worth keeping: the availability API is not a permission check.**
> `get-foundation-model-availability` still reports `authorizationStatus:
> AUTHORIZED` and `entitlementAvailability: AVAILABLE` for these models *right
> now*, while every invoke is rejected. Those fields describe region and
> entitlement, not whether a call will be accepted. **Only an invoke tells the
> truth** — which is the one reason the original error was caught at all.
>
> **Fixed by** submitting the Anthropic use-case form once, per account, via
> `aws bedrock put-use-case-for-model-access --form-data <blob>` (or the
> console's Bedrock → Model access page). The blob is a customer profile —
> company, website, industry, intended use — which must be truthful, so it is
> the account owner's to submit rather than something to fabricate.
>
> The error advises waiting 15 minutes; in practice approval landed in ~1 minute.
> `intendedUsers: "0"` was accepted without issue — a solo project does not need
> to inflate the number.
>
> **If a future account hits this**, the tell is that
> `get-use-case-for-model-access` errors with "You have not filled out the
> request form" while `get-foundation-model-availability` reports `AUTHORIZED`.
> Trust the former.

The rest of this section (ARNs, profile routing, IAM shape) is unaffected — it
describes what to grant, and remains correct once the form clears.

### Current-generation Anthropic models are inference-profile only

`list-foundation-models` reports `inferenceTypesSupported: INFERENCE_PROFILE`
for every current model — Opus 5, Sonnet 5, Sonnet 4.5, Haiku 4.5 and the rest.
Only the legacy Claude 3.x models still carry `ON_DEMAND`. **The bare model ID is
not invocable**; calls must go through an inference profile.

There are no `apac.` profiles for the current generation either — those exist
only for Claude 3.x and Sonnet 4. The current models are reachable via `global.`
profiles, which is what the two rungs below use.

### The two benchmark rungs (PLAN.md §1d)

| Rung | Profile ID |
|---|---|
| Cheap | `global.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Quality | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` |

### IAM — three ARNs per model, and why `*` is still avoidable

This is the part that would have been discovered the hard way. Granting
`bedrock:InvokeModel` on the profile ARN **alone** is not sufficient: the caller
also needs it on each foundation-model ARN the profile routes to. Each of these
profiles routes to two, one region-agnostic and one region-scoped:

```
# Haiku 4.5 (cheap rung)
arn:aws:bedrock:ap-southeast-1:149751500899:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0
arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0
arn:aws:bedrock:ap-southeast-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0

# Sonnet 4.5 (quality rung)
arn:aws:bedrock:ap-southeast-1:149751500899:inference-profile/global.anthropic.claude-sonnet-4-5-20250929-v1:0
arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0
arn:aws:bedrock:ap-southeast-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0
```

Note the empty account field on the foundation-model ARNs — they are AWS-owned,
not account-owned, which is why they read `:::` and `ap-southeast-1::`.

**The plan's "scope to the specific model ARN, not `*`" survives this**, which
was not guaranteed: a `global.` profile *could* have fanned out to a dozen
regional model ARNs and made an explicit list impractical. These two fan out to
two each. Enumerate all six; do not reach for a wildcard.

**Consequence for the benchmark.** Listing both rungs means the execution role
can invoke either. If that is uncomfortable, split it — grant the cheap rung
permanently and the quality rung only while benchmarking — but the simpler
honest answer is that both are named, both are scoped, and neither is `*`.

### Not yet verified

The invoke above was made by the `terraform-pipelineguard` IAM user, which
proves **account-level** access — no SCP or entitlement blocks these models. It
does *not* prove the AgentCore execution role will work, since that role does not
exist yet. Re-run the same `converse` call from the execution role once Phase 1a
applies.

---

## 12. AgentCore in `ap-southeast-1` — verified from the API

Previously asserted from a web search. Now confirmed against the control plane
(`aws bedrock-agentcore-control`, CLI 2.35.19):

| Check | Result |
|---|---|
| Control plane reachable in region | ✅ `list-agent-runtimes` returns `[]` |
| `aws.browser.v1` | ✅ **READY** |
| `aws.codeinterpreter.v1` | ✅ **READY** |

Both tools the QA agent needs (PLAN.md 1b) are system-provided and live in the
chosen region. `list-browsers`/`list-code-interpreters` return empty because
those list *custom* instances; the system ones are fetched by id.

### Two findings from the `create-agent-runtime` contract that change the plan

**1. A container image is not required. `--agent-runtime-artifact` is a tagged
union:** `containerConfiguration` (ECR URI) **or** `codeConfiguration` — an S3
bucket + prefix, a `runtime` enum, and an `entryPoint`. Supported runtimes
include `PYTHON_3_10` … `PYTHON_3_14` and `NODE_22`.

This makes PLAN.md's ECR repo, `scripts/deploy-qa-agent.sh`, the
`--platform linux/arm64` requirement, the silent-amd64-rejection trap, and the
3-phase cold apply **all unnecessary**. Zip the agent, put it in S3, point the
runtime at it.

It also mirrors a split this repo already makes: the **cost gate is a zip
Lambda** and the **security gate is a container**, and the reason is size —
Trivy + Checkov are ~320 MB. A Python agent using the Claude Agent SDK is small,
so it belongs on the zip side, exactly like the cost gate. Take
`codeConfiguration` and delete the container work.

> **Trap to avoid when doing so:** the agent code must **not** live in the
> reports bucket. That bucket has a 7-day expiry lifecycle (§ PLAN.md 1a), which
> would silently delete the agent's own code and break the runtime a week after
> it starts working. Use a separate bucket, or a prefix explicitly excluded from
> the lifecycle rule.

**2. `--network-configuration` is required, and `networkMode` is `PUBLIC` or
`VPC`.** Choose **`PUBLIC`**. The known provider issue where
`aws_bedrockagentcore_agent_runtime` leaves undeletable ENIs and hangs `destroy`
on a dependency cycle is a **VPC-mode** problem — ENIs only exist there. This
project destroys and rebuilds routinely, so picking `PUBLIC` removes that
failure mode by construction rather than documenting a workaround for it.

`PUBLIC` is also correct on the merits: the agent's targets are a public tunnel
URL and public AWS APIs. It needs no VPC reachability.

---

## 13. What a real apply settled — AgentCore runtime, verified 2026-08-26

`module.qa_agent` was applied and the runtime invoked for real. Four things were
guesses before this; all four are now facts, and two of them were **wrong**.

### Confirmed

| Question | Answer |
|---|---|
| Managed code runtime architecture | **aarch64** — an `manylinux2014_aarch64` / `cp312` zip loaded and ran |
| `entry_point` contract | **Command array** `["agent.py"]`, module exposing `app` — as read from the toolkit, not the docs |
| Runtime Python | `3.12.12`, at `/opt/aws/agentcore-runtime/python/versions/3.12.12/` |
| Code unpack location | `/var/task/` — Lambda-like, as the packaging model implies |
| Browser tool end-to-end | Session start → SigV4 websocket → CDP → clean stop |

### Wrong #1 — the log group name, and worse, its retention

The module guessed `/aws/bedrock-agentcore/<name_prefix>`. **Nothing ever wrote
to it.** AgentCore writes to a group it names itself:

```
/aws/bedrock-agentcore/runtimes/<agent_runtime_id>-DEFAULT
```

The name was the smaller problem. The group AgentCore creates has
**`retentionInDays: null` — logs never expire**, on a project where every other
log group is capped at 7 days with a written justification. An unnoticed
unbounded-growth cost.

**The fix, and the part worth knowing:** deleting AgentCore's group and letting
Terraform create one with the same name works — AgentCore writes into a
pre-existing group rather than insisting on making its own. Verified by invoking
afterwards and reading the traceback out of the Terraform-managed group. The
name depends on the runtime's server-generated id, so the resource is gated on
the same `count` as the runtime and derives its name from that attribute.

### Wrong #2 — the IAM runtime ARN pattern

A real runtime ARN is:

```
arn:aws:bedrock-agentcore:ap-southeast-1:149751500899:runtime/pipelineguard_qa_dev_runtime-W2kNEGDSFW
```

**Underscores**, because `agentRuntimeName` must match
`[a-zA-Z][a-zA-Z0-9_]{0,47}` — the only resource in this repo that rejects its
hyphenated naming convention. The name was transformed correctly; the OIDC
role's `InvokeAgentRuntime` resource pattern was **not**, and still read
`runtime/pipelineguard-qa-dev-*`. It would never have matched, and the QA
workflow would have failed its first invoke with AccessDenied pointing at the
runtime rather than at the policy. Both now derive from one `local.runtime_name`.

### Missing — the browser tool permission

The execution role granted Bedrock, S3, KMS, Secrets, logs and CloudWatch, and
**never granted access to the tool the agent exists to drive**. Found only by
invoking:

```
AccessDeniedException ... bedrock-agentcore:StartBrowserSession on
arn:aws:bedrock-agentcore:ap-southeast-1:aws:browser/aws.browser.v1
```

Note the account field is literally `aws` — a service-owned resource, the same
shape as the foundation-model ARNs in §11. Four actions are needed:
`StartBrowserSession`, `StopBrowserSession`, `GetBrowserSession`, and
`ConnectBrowserAutomationStream` for the SigV4-signed websocket at
`/browser-streams/<id>/sessions/<sid>/automation`.

**Code Interpreter is deliberately not granted.** PLAN.md 1b lists it as
available, but this agent never calls it, and an unused grant is one to justify
in review for no benefit.

### Resolved afterwards — `ReadAgentCode` was NOT needed

Removed the statement, forced a runtime replacement so the artifact had to be
fetched again, and both creation and invoke succeeded. **AgentCore reads the
deployment zip under its own service principal, not the execution role.** The
grant is gone.

### Corrected afterwards — Terraform cannot own the log group at all

The fix recorded above ("delete AgentCore's group, let Terraform create one")
was a **misleading half-success**. It works only when the runtime already
exists. On a fresh runtime, AgentCore creates the group before Terraform can,
and the apply fails:

```
ResourceAlreadyExistsException: The specified log group already exists
```

This is structural, not a race worth retrying: the group name contains the
runtime's server-generated id, so the Terraform resource cannot exist until the
runtime does — by which point AgentCore has already made the group.

Terraform therefore sets the **retention** rather than owning the group, via a
`terraform_data` + `local-exec` calling `put-retention-policy`. It is idempotent,
and its trigger includes the runtime id so a replacement re-applies retention —
which matters, because a replacement means a brand-new group defaulting to
"never expire" again.

### What is running, and what it costs

Applied: 2 S3 buckets, 1 secret, 2 IAM roles, 1 log group, 1 runtime, and the
shared CMK pulled in as a dependency. Ongoing ≈ **$1.40/month** — KMS $1,
Secrets Manager $0.40, everything else effectively zero. An idle AgentCore
runtime bills per session, not for existing. No NAT, ALB, or Fargate.

Tear down with `terraform destroy -target=module.qa_agent`.

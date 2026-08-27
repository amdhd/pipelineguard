# PipelineGuard → AgentCore UI-QA + Bug-Fix Loop

A phased build plan. Each phase is independently shippable and demoable.
**Do not skip Phase 0.5.** Do not start a later phase until the exit criteria of
the previous one are met.

> **Revision note.** This is the second draft. The first was written against a
> different reference implementation (a Cognito/Amplify app) and assumed the QA
> target lived in this repo. It does not. Every assumption below has been checked
> against the actual repositories; findings that changed the plan are called out
> inline as **[verified]** or **[corrected]**.

---

## The actual system

This is a **two-repository, two-region** build. The original draft assumed one of
each, and most of its structural errors follow from that.

| | PipelineGuard (this repo) | vesselAI (the QA target) |
|---|---|---|
| Repo | `amdhd/pipelineguard` (public) | `amdhd/vesselAI` (public) |
| Role | Owns the AgentCore runtime, IAM, S3, cost controls | Owns the app under test **and the QA workflow** |
| Region | `ap-southeast-1` | `us-east-1` |
| Account | `149751500899` | **`149751500899` — the same.** Confirmed in Phase 0 |
| Runtime | ECS Fargate + ALB, driven by CodePipeline | EKS 1.36 + ALB + ACM, built by Terraform |
| TF provider | `aws ~> 5.0` (locked `5.100.0`) → **upgrading to `~> 6.0`** | `aws ~> 5.60`, unchanged |
| TF state | S3 + lockfile | **local, deliberately** — left as-is |

**The QA target is not the EKS cluster.** **[revised]** An earlier draft
provisioned vesselAI's EKS cluster per run. That was dropped — see
"Target environment" below. vesselAI's AWS account is therefore **not touched by
the QA loop at all**, which is why its local-state decision, and the reasoning
written into `terraform/versions.tf`, stay intact.

**Why vesselAI is the right target.** Its README, at the "contract audit"
paragraph, describes going module-by-module through the live app *by hand* and
finding roughly a third of it silently broken — frontend/backend contract drift
rendering `NaN`, blank charts, and unguarded React crashes rather than clean
404s. That is a manual UI-QA audit, already performed and already written up.
Automating it is the entire value proposition, and it gives the rubric a corpus
of *known real* findings to be measured against.

**What it offers as a target:** React 18 + Vite, six module dashboards, Leaflet
maps, Recharts, an SSE chat surface and an agent-trace UI; JWT auth with demo
credentials; Vitest across frontend, backend and components; 21 dbt data tests.

### Target environment — `docker-compose.prod.yml`, not EKS

**[revised]** The QA loop runs the app from vesselAI's own
`docker-compose.prod.yml` inside the GitHub runner, exposed to AgentCore Browser
through an ephemeral tunnel. It does **not** provision the EKS cluster.

That compose file turns out to be almost purpose-built for this:

- **Single origin, single port.** Only `frontend` publishes a port (`8080`).
  nginx serves the SPA and reverse-proxies `/api/` → backend:3001,
  `/api/analytics/` → analytics-api:8000, and `/socket.io/` → backend. One
  tunnel to `:8080` exposes the whole application. `VITE_API_URL` is already the
  relative `/api`, so there is no chicken-and-egg where the frontend build needs
  to know the tunnel hostname before it exists.
- **It seeds itself, deterministically.** The `seed` service upserts the demo
  fleet, user and vessels; its own comment notes that without it "the credentials
  the login page advertises would be rejected and the deployed app would be
  unusable." A fresh database per run is *better* than a long-lived cluster for
  QA — no state pollution between rounds, and demo login is guaranteed.
- **Half the health gate already exists.** `service_healthy` on Postgres,
  `service_completed_successfully` on `migrate` and `seed`, so
  `docker compose up --wait` blocks until the stack is genuinely ready.
- **Production-style artifacts.** Multi-stage non-root images, secrets from the
  environment, migrations as a one-off. This is not a toy fallback mode.

**Why not EKS.** Chromium cannot observe what is behind the URL, and the bug
class being hunted — frontend/backend contract drift, `NaN` renders, blank
charts, unguarded React crashes — is orthogonal to the orchestrator. It
reproduces identically under compose. "Test what you ship" has no referent here
either: vesselAI's README lists three deployment options *"in increasing order of
realism"* and says the cluster lives for three days. There is no production for
EKS to be faithful to.

| | EKS on demand | compose + tunnel |
|---|---|---|
| Time to testable URL | 20–30 min | 3–5 min |
| Cost per run | ~$0.37/hr + stranded-cluster risk | runner minutes (free, public repo) |
| Teardown | Ordered finalizer dance; can strand an ALB | Runner evaporates |
| Prerequisites | Remote state, Route 53 zone, ACM, external-dns | None |

**Where EKS still belongs.** vesselAI's README is explicit that the EKS
migration surfaced **nine bugs a laptop cluster could not have**, and compose
genuinely cannot catch that class: ingress rules, sealed-secret injection,
resource limits causing OOM, readiness-probe misconfiguration, Argo CD sync
drift. So EKS is not discarded — it is matched to the right trigger frequency:

- **Per-PR runs → compose + tunnel.** Fast, free, per-commit.
- **EKS QA → manual and occasional**, run deliberately when validating the
  deployment path itself. Kept out of CI, so vesselAI needs no remote state.

**The honest drawback.** A quick tunnel is a third-party dependency in the
critical path, and it is an unauthenticated public URL for the life of the job.

**[corrected]** An earlier revision waved this away as low-stakes "because the
demo credentials are already public." That reasoning does not transfer. Public
demo credentials expose seeded demo data; the tunnel also exposes whatever
**live, billable** credentials the stack is running with — principally
`ANTHROPIC_API_KEY`, which is server-side and metered. `trycloudflare.com`
hostnames are discoverable and actively scanned, so "nobody will find it in four
minutes" is not a control. See Phase 0.5 #4: the fix is to default the stack to
a dummy AI key so there is nothing worth reaching, not to argue the window is
small. Keep the tunnel's lifetime bounded by the job, and record both notes
together in the runbook.

---

## Prime directives (read before every phase)

1. **Cost is a hard constraint, not a nice-to-have.** Every agent invocation
   costs tokens, and — see directive 2 — this build also spends on infrastructure
   per run. Cost controls are built in Phase 1, not bolted on later.

2. **Wall-clock is a cost, not just tokens.** **[corrected]** The original
   draft's controls were all token-shaped. AgentCore Browser bills
   ~$0.0895/vCPU-hour and ~$0.00945/GB-hour, and *memory is billed on peak
   footprint for every second the session is alive, including idle*. A hung
   session costs money while generating no tokens. Every run therefore needs a
   **wall-clock cap**, not just a token budget.

   *(An earlier draft carried a second dimension — target infrastructure at
   ~$0.37/hr for an EKS cluster, and the $267/month a forgotten one costs. That
   is gone with the move to compose; the target now costs runner minutes. It
   returns the moment anyone reintroduces a cloud-provisioned target, so the
   number is recorded here rather than deleted.)*

3. **Never auto-modify infrastructure.** The bug-fix agent must never touch
   Terraform, workflows, buildspecs, deployment scripts, k8s manifests, IAM
   policy documents, or secrets. Enforced by an explicit deny-list, checked
   before any patch is applied. See Phase 2 for the full list — the original
   draft's was too narrow.

4. **Human approval is non-negotiable.** All agent output lands as a PR against a
   protected branch. No agent commit reaches `main` without human review.
   **[corrected]** `main` is currently **unprotected on both repos**, and both
   are **public**. This is a Phase 0.5 prerequisite, not an assumption.

5. **Verify the deploy before invoking any agent.** Assert the target is healthy
   first — and under directive 2 this is not merely about wasting a round. An
   agent started against a half-ready stack burns browser-session wall-clock
   while the app 502s. With compose the window is short, but `migrate` and
   `seed` still have to complete before a login will succeed, so the gate is
   about *readiness*, not just reachability.

6. **Anything the agent emits that is not valid JSON is a bug in the prompt,**
   not a finding. Reject and log; never parse free text into a findings table.

7. **The target is ephemeral and lives inside the job.** **[revised]** With
   compose in the runner there is nothing to strand: the runner dies and takes
   the stack, the volume and the tunnel with it. This directive existed to guard
   a $0.37/hr cluster and is now mostly discharged by construction — which is
   itself the strongest argument for the design. What remains is narrow: bound
   the tunnel's lifetime to the job, and never write the target's URL anywhere
   durable.

---

## Phase 0 — Discovery

**Goal:** a written map of both repos so later phases build on what is actually
there. Most of this is already done; the task is to record it in
`docs/agentcore/DISCOVERY.md` with real file and resource names.

Already established **[verified]**:

**PipelineGuard**
- `infra/` is a flat root module (`main.tf`, `variables.tf`, `outputs.tf`,
  `kms.tf`, `versions.tf`) with five child modules under `infra/modules/`:
  `networking`, `ecr`, `ecs`, `pipeline`, `gates`. Naming convention throughout
  is `pipelineguard-<thing>-<environment>`.
- State: S3 backend, `pipelineguard-tfstate-149751500899-ap-southeast-1`, key
  `pipelineguard/dev/terraform.tfstate`, `use_lockfile = true`. Config in
  `infra/backend.conf` (git-ignored). Variables in `infra/environments/dev.tfvars`.
- Gates are two Lambdas sharing one IAM role (`pipelineguard-gate-lambda-dev`),
  invoked from CodeBuild stages: **cost gate** is zip + an Infracost layer;
  **security gate** is a **container image** (Trivy + Checkov exceed the 250 MB
  zip limit). Both read `pipelineguard/gates/dev` from Secrets Manager and both
  post to Slack.
- **No GitHub OIDC provider exists here.** `ci.yml` is deliberately AWS-free
  ("Nothing here touches AWS — no credentials, no backend, no apply").
  Introducing an AWS-assuming workflow is a posture change; record it as such.
- ECR: `pipelineguard-<env>` (immutable, SHA-tagged) for the app;
  `pipelineguard-security-gate-<env>` (mutable, `:latest`) for the gate image.
- Orchestrator is **both**: CodePipeline in AWS post-merge, GitHub Actions as a
  pre-merge mirror. The Slack webhook lives in Secrets Manager under
  `SLACK_WEBHOOK_URL`, read by the Lambda handlers — **not** a GitHub secret.
- Reusable as-is: `gates/security_gate/github_commenter.py` (including
  `resolve_pr_number`, which maps a merge commit back to its originating PR) and
  the `_notify_slack` helpers in both handlers.

**vesselAI**
- `frontend/` (React 18 + Vite — **the QA target**) and `frontend-angular/` (a
  second, separate frontend — *not* the target; record this so the route
  allow-list is unambiguous). Plus `backend/`, `data-platform/`, `k8s/`,
  `terraform/`.
- `terraform/` builds 64 resources in ~20 minutes: VPC, six ECR repos, EKS 1.36,
  a managed node group, five IAM roles, ACM wildcard cert, KMS key. Driven by
  `terraform/Makefile` (`make apply`, `make destroy`).
- **A GitHub OIDC provider already exists** in `terraform/github-oidc.tf`, with a
  `StringEquals` pin to `repo:amdhd/vesselAI:ref:refs/heads/main` and a written
  rationale for why `StringLike` wildcards are dangerous. Reuse the provider;
  the `sub` condition needs widening (Phase 0.5).
- Domain `ahmadhadi.org` via Route 53 + external-dns. **Currently no DNS
  resolves** — the hosted zone is down along with the cluster.
- Demo credentials are **published in the public README**
  (`demo@petronas.com` / `demo123`). Secrets Manager is still the right pattern
  to demonstrate, but say plainly in DISCOVERY.md that it is convention here, not
  protection of a real secret. Do not overclaim it in an interview.

**Decisions recorded** (made before Phase 1; amend DISCOVERY.md if they change):

| Decision | Choice | Why |
|---|---|---|
| QA target | vesselAI `frontend/` (React) | Real UI, real routes, real known-bug corpus |
| Target hosting | **`docker-compose.prod.yml` in the runner + tunnel** | Single origin, self-seeding, no cloud target to strand |
| EKS QA | **Manual and occasional**, never in CI | Catches k8s-specific drift at the right frequency |
| Workflow location | **vesselAI** | PR comments and fix PRs land natively — no cross-repo token |
| AgentCore location | **PipelineGuard**, `ap-southeast-1` | Reusable agent infra is this repo's contribution |
| Terraform vs CDK | **Terraform** | `aws_bedrockagentcore_agent_runtime` exists — see below |

> **Terraform vs CDK — the note the first draft got wrong.** **[corrected]** It
> said provider support "may lag" and to check. It no longer lags:
> `aws_bedrockagentcore_agent_runtime` and `aws_bedrockagentcore_agent_runtime_endpoint`
> ship in the AWS provider, and `awscc_bedrockagentcore_runtime` in AWSCC. The
> real constraint is different and specific: **they are v6.x resources, and this
> repo is pinned `~> 5.0` at `5.100.0`.** The decision is therefore not
> Terraform-vs-CDK but *provider major upgrade* — taken, see Phase 0.5. Note
> also the open provider issue where `aws_bedrockagentcore_agent_runtime` leaves
> undeletable ENIs and hangs `destroy` on a dependency cycle; that matters
> directly to this project's on/off teardown workflow and belongs in the runbook.

> **Region availability.** **[verified]** AgentCore Browser is available in nine
> regions including **ap-southeast-1**. No cross-region hop is needed for the
> agent. Record it as checked rather than leaving it a risk.

**Exit criteria:** `DISCOVERY.md` exists and names real files and real resource
names. No invented paths anywhere in it.

---

## Phase 0.5 — Hard prerequisites *(new — none of this exists yet)*

The original draft assumed all of these. None hold. Each is small; together they
are the difference between the plan working and the plan not starting.

**[revised]** This list was five items when the target was EKS. Two are gone,
because with compose in the runner the QA loop never touches vesselAI's AWS
account:

- ~~Remote Terraform state for vesselAI~~ — not needed. The deliberate
  local-state decision in `terraform/versions.tf`, and its written rationale,
  stay exactly as they are.
- ~~Widen vesselAI's OIDC `sub` condition~~ — not needed. Its existing provider
  is for publishing images from `main` and is untouched. The only OIDC trust the
  QA loop needs is the *new* role in PipelineGuard's account (Phase 1a), whose
  `sub` list includes vesselAI's `pull_request` subject from the start.

1. **Branch protection on `main`, both repos.** Directive 4 depends on it.
   Require a PR, require CI to pass, no force-push. The QA workflow must use
   `pull_request`, never `pull_request_target`.

   **[corrected]** This item used to add "confirm fork PRs cannot reach
   secrets." That is true and *beside the point*: `pull_request` withholds
   secrets, but the credential this design actually cares about is the **OIDC
   token**, which is not a secret and is governed by a different mechanism
   entirely. The real control is the fork guard in Phase 1a — check that, not
   the secrets setting.

2. **Upgrade PipelineGuard to `aws ~> 6.0`.** Required for the AgentCore
   resources. Do it as its own commit against the existing stack, before any
   agent work: `terraform plan` must come back clean, `terraform fmt -check` and
   `tflint` must pass, and Checkov's **failed and skipped counts must not move**.

   **[corrected during execution]** This previously read "Checkov must still
   report 172 passed / 0 failed / 31 skipped." The current Checkov (3.2.459)
   reports **166 / 0 / 31**, and it reports 166 on the *unmodified* pre-upgrade
   tree too — so the 172 in the memory of this project is a stale figure from an
   older Checkov, not drift introduced here. Checkov parses HCL directly and
   never loads the provider, so a provider version bump **cannot** change its
   results by construction; pinning an expected *passed* count is pinning
   Checkov's release cadence, not this repo's posture. Assert on **failed = 0 and
   skipped = 31** instead, which are the numbers that actually describe the
   configuration.

   **Pin deliberately, and lower-bound it.** `aws_bedrockagentcore_agent_runtime`
   landed in **6.18.0** and is under active churn — `agent_runtime_artifact` is
   still gaining source types (an S3 zip alternative to the ECR container arrived
   after the initial resource). A bare `~> 6.0` would satisfy `terraform init`
   with 6.0.0 and then fail on an unknown resource type, which is a confusing
   error to debug. Use `>= 6.18, < 7.0`, keep `.terraform.lock.hcl` committed as
   it already is, and treat provider bumps as their own reviewable commits rather
   than incidental drift.

3. **Enable Bedrock model access** in `149751500899` / `ap-southeast-1`.
   **[corrected]** The first draft assumed Bedrock. This repo has **zero Bedrock
   usage** — `gates/security_gate/claude_summariser.py` calls the Anthropic API
   directly with a key from Secrets Manager. AgentCore Runtime forces Bedrock, so
   this is a genuinely new dependency: request model access for the chosen model
   and record the model ARN, because the IAM policy scopes to it.

4. **An `ANTHROPIC_API_KEY` for the compose stack** *(new)*. The compose stack
   declares it required with no default (`${ANTHROPIC_API_KEY:?}`), so the
   backend will not start without one. vesselAI's AI paths all have
   deterministic fallbacks that flag `fallback: true`, so a *dummy* key still
   yields a fully running app with every AI surface in fallback mode.

   **Default to the dummy key. [corrected]** An earlier revision said to use a
   real key "for normal runs." That was wrong, and for a reason that does not
   transfer from the demo-credentials argument: `demo@petronas.com` /
   `demo123` are already public and grant access to seeded demo data, whereas
   **the Anthropic key is a live, server-side, billable credential**. Behind an
   unauthenticated public tunnel it is a spend liability in two directions:

   - **Outward.** `trycloudflare.com` hostnames are discoverable and actively
     scanned. Anyone who reaches the tunnel during the job can hit the chat and
     SSE endpoints and burn your Anthropic spend.
   - **Inward, and easier to miss.** The QA agent itself exercises those chat
     surfaces on every run. With a real key that is invisible, recurring,
     unbudgeted spend on a project whose first prime directive is cost.

   So:
   - **`pull_request` and `schedule` runs → dummy key.** The fallback path is
     deterministic, flagged, and costs nothing — and this plan already wants it
     tested. The rubric must be told it is in fallback mode, or every AI surface
     becomes a false positive.
   - **Real key only behind an explicit `workflow_dispatch` input.** A
     deliberate, attended choice, exactly like the label gate.
   - **Put a monthly spend cap on the key regardless**, in the Anthropic
     console. Belt and braces, and it is one click.

   Record this next to the tunnel note in the runbook — the two risks share a
   root cause and should be read together.

**Exit criteria:** all four done, each as its own reviewable commit. The existing
PipelineGuard stack still applies, destroys, and passes its gates unchanged.

---

## Phase 1 — QA agent only (no auto-fix)

This phase alone is a complete, demoable project. Most of the value lives here.

### 1a. Infrastructure — PipelineGuard, `ap-southeast-1`

New module `infra/modules/qa_agent/`, matching the existing convention
(`pipelineguard-<thing>-<environment>`), wired from `infra/main.tf` and given the
shared CMK from `infra/kms.tf` like every other module.

- **S3 bucket** for screenshots and QA reports. Lifecycle rule: expire after
  7 days. Block public access — this repo is public and screenshots of the target
  app should not be. SSE-KMS with the shared CMK.
- **Secrets Manager secret** for the QA target's login. Follow the existing
  pattern in `infra/modules/gates/main.tf`: **Terraform owns the container, never
  the material**, seeded out-of-band by a script alongside
  `scripts/seed-gate-secrets.sh`. Never a `TF_VAR_*` — `terraform show -json`
  does not redact, and `plan.json` ships to S3 as a pipeline artifact.
- **IAM execution role** for the AgentCore runtime, least-privilege. Be able to
  justify every statement in an interview:
  - `bedrock:InvokeModel` scoped to the **specific model ARN** — not `*`
  - `s3:PutObject` scoped to the screenshot bucket prefix only
  - `secretsmanager:GetSecretValue` scoped to the one secret ARN
  - `kms:Decrypt`/`GenerateDataKey` on the shared CMK only
  - CloudWatch Logs write, and `cloudwatch:PutMetricData` scoped by namespace
  - Nothing else.
- **GitHub OIDC role — reusing the existing provider. [corrected by Phase 0]**
  This bullet previously read "vesselAI's provider is in a different account and
  cannot be reused across the boundary," and told Phase 0 to confirm. **Phase 0
  confirmed the opposite: both repos are in account `149751500899`**
  (`~/.aws/config` records `login_session = arn:aws:iam::149751500899:user/vessel-k8`
  for the `vesselmind` profile). See [DISCOVERY.md §1](DISCOVERY.md).

  So **create only a role, never a provider.** IAM allows one OIDC provider per
  issuer URL per account, vesselAI's `terraform/github-oidc.tf` already declares
  one for `token.actions.githubusercontent.com`, and a second would fail the
  apply with `EntityAlreadyExists`. IAM is global, so that provider is usable
  from `ap-southeast-1` unchanged. Reference its ARN via a variable or a
  `data "aws_iam_openid_connect_provider"` lookup — do **not** manage it here,
  or two Terraform states will fight over one resource.

  This role exists solely so vesselAI's workflow can invoke the runtime, so
  scope it to `bedrock-agentcore:InvokeAgentRuntime` on the one runtime ARN plus
  read on the one secret. Note that the same-account finding removes an account
  boundary this plan had assumed as a backstop — the role a public repo's CI can
  assume now lives alongside the live PipelineGuard stack, which makes the
  minimal scope below load-bearing rather than merely tidy.

  > **This scope must reconcile with what 1c actually does. [corrected]** An
  > earlier revision had 1c step 6 archiving findings JSON to S3 and publishing
  > CloudWatch metrics, while this role granted neither — the workflow would have
  > failed with AccessDenied on its first successful run. There are two ways to
  > close that gap, and they are not equivalent:
  >
  > - **Widen this role** with `s3:PutObject` on the reports prefix and
  >   `cloudwatch:PutMetricData`. Straightforward, but it grows the one role
  >   reachable from a public repo's CI.
  > - **Move the writes into the runtime's execution role** — recommended. That
  >   role *already* has `s3:PutObject` on the bucket prefix and CloudWatch
  >   write, because the agent writes screenshots there anyway. Let the agent
  >   also write its findings JSON and emit its own token/session metrics; the
  >   harness receives the findings in the invoke response and needs **no AWS
  >   write permission at all**.
  >
  > Take the second. It keeps the CI-reachable role at exactly two statements —
  > invoke one runtime, read one secret — which is a far better answer to "justify
  > every statement" than a role that has quietly accumulated writes. Runner
  > minutes then go in the comment *text* rather than a CloudWatch metric, since
  > that is the one figure the agent cannot know; promote it to a metric later by
  > widening deliberately, if it ever earns it.

- **Screenshot delivery.** **[added]** The bucket is block-public-access, so a
  bare S3 key in a PR comment is unopenable for a reviewer — the evidence for
  every finding would be inaccessible to the person meant to act on it, which
  quietly defeats the report. Pick one and record it:

  - **Presigned URLs — recommended.** The agent generates them as it uploads
    (its role needs `s3:GetObject` plus `kms:Decrypt` on the CMK to sign for an
    SSE-KMS object) and returns them in the findings JSON, so the harness still
    needs no S3 access. Cap the expiry at the bucket's 7-day lifecycle so a link
    never outlives its object. **Name the exposure:** vesselAI's repo is public,
    so a presigned URL in a PR comment is effectively a public screenshot for its
    validity window. For a demo app whose credentials are already published that
    is acceptable — but it is a decision, not a detail.
  - **GitHub Actions artifacts** — upload screenshots as a run artifact instead.
    Zero public exposure and no extra IAM at all; reviewers download a zip rather
    than seeing images inline. Take this one if the exposure above is unwelcome,
    or if the target ever becomes an app with real data.

  **`StringEquals` on an enumerated subject list, never `StringLike`** — the
  same discipline vesselAI's own `github-oidc.tf` argues for at length, and the
  reason this plan does not simply write `repo:amdhd/vesselAI:*`. The workflow
  has three triggers, so enumerate what they actually present:
  `repo:amdhd/vesselAI:pull_request` for PR runs, and
  `repo:amdhd/vesselAI:ref:refs/heads/main` for `schedule` and
  `workflow_dispatch`.

  > **The subject list does NOT exclude fork PRs. [corrected]** An earlier
  > revision of this plan claimed it did — "anything not on that list cannot
  > assume the role, a fork's PR included." That was **wrong**, and the error is
  > worth keeping visible because it is a common one.
  >
  > For `pull_request` events GitHub mints the token with
  > `sub = repo:amdhd/vesselAI:pull_request` for **fork and same-repo PRs
  > alike**. The claim carries no fork/non-fork signal, so the trust policy
  > cannot distinguish them and contributes nothing to this particular defence.
  > Compounding it, a `pull_request` workflow runs the workflow file **from the
  > PR's own merge ref** — so a malicious fork PR can edit `ui-qa-agent.yml`
  > itself and append steps.
  >
  > What actually blocks this today is a **platform default, not this policy**:
  > on a public repo GitHub caps fork-PR permissions at read, so `id-token:
  > write` is never granted and no OIDC token is issued. That is a setting, and
  > the admin option which lifts it applies to **private** repos — which this
  > plan already contemplates elsewhere. Inheriting a protection you do not
  > control while documenting it as one you enforce is how this becomes a real
  > finding later.
  >
  > Note also that Phase 0.5's "confirm fork PRs cannot reach secrets" does not
  > cover this. It is true and beside the point: `pull_request` correctly
  > withholds *secrets*, but an OIDC token is not a secret. The `agent-qa` label
  > gate helps — a fork author cannot self-label — but collapses the moment a
  > maintainer labels a fork PR to review it, which is the normal thing to do.
  >
  > **The cost of abuse is concrete:** runtime invocation burn and
  > `secretsmanager:GetSecretValue` against the QA secret.

  **So state the protection explicitly, in two layers:**

  1. **A workflow-level fork guard — primary, and unambiguous.** On the job that
     assumes the role:
     `if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.fork == false`
  2. **A `job_workflow_ref` condition** in the trust policy for the non-PR
     triggers, pinning `schedule` and `workflow_dispatch` to
     `...ui-qa-agent.yml@refs/heads/main`. **Do not blanket-apply this to
     `pull_request`**: for PR runs the claim's ref is the *merge* ref, so a
     `StringEquals` on `main` would reject legitimate PRs. Verify the actual
     claim value in Phase 1a before pinning anything, and rely on layer 1 for
     the PR path. That is the same "enumerate what they actually present"
     discipline this plan preaches — it simply enumerated `pull_request` wrong.

  **Confirm in Phase 0 whether the two repos share an account**; if they do,
  extend the existing provider rather than creating a second one (an account may
  hold only one provider per issuer URL).
- **Code bucket for the agent zip** *(new — replaces the ECR repo, see below)*.
  `pipelineguard-qa-<env>-code-<account>`. Versioned, SSE-KMS on the shared CMK,
  public access blocked, and — the point — **no expiry lifecycle rule**.

  > **This must not be the reports bucket.** That bucket expires *everything*
  > after 7 days (`filter {}`), so agent code parked there would be deleted a
  > week after it started working, breaking the runtime with no recent change to
  > blame. A separate bucket makes that impossible by construction; sharing one
  > and excluding a prefix leaves the rule one careless edit from doing it.

  > ### No container image. **[corrected — verified against the API]**
  >
  > This bullet used to specify an ECR repo, an `arm64` build script, and a
  > 3-phase cold apply, on the assumption that AgentCore Runtime takes a
  > container. It takes either. `agent_runtime_artifact` is a **tagged union**:
  > `container_configuration` (an ECR URI) **or** `code_configuration` — an S3
  > bucket and prefix, a `runtime` enum, and an `entry_point`. Supported runtimes
  > include `PYTHON_3_10`…`PYTHON_3_14` and `NODE_22`.
  >
  > **Take `code_configuration`.** It deletes, in one decision: the ECR
  > repository, `scripts/deploy-qa-agent.sh`, the `--platform linux/arm64`
  > requirement, the silent-amd64-rejection trap this plan was about to write
  > guards against, and the 3-phase cold apply ordering.
  >
  > It also matches a split this repo already makes and can justify: the **cost
  > gate is a zip Lambda**, the **security gate is a container**, and the
  > deciding factor is size — Trivy plus Checkov are ~320 MB. A Python agent
  > using the Claude Agent SDK is small. It belongs on the zip side, exactly like
  > the cost gate.

- **AgentCore runtime** via `aws_bedrockagentcore_agent_runtime`. Schema below is
  read from `terraform providers schema -json` on the pinned provider, not from
  documentation:

  ```hcl
  agent_runtime_artifact {
    code_configuration {
      runtime     = "PYTHON_3_12"
      entry_point = ["handler.invoke"]
      code {
        s3 {
          bucket = <code bucket>
          prefix = <key of the uploaded zip>
        }
      }
    }
  }

  # Required. PUBLIC, deliberately -- see below.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # NOTE: an ATTRIBUTE (list of object), not a block. A `lifecycle_configuration
  # { ... }` block -- which an earlier draft of this plan showed -- does not
  # parse. The schema is ["list", ["object", {...}]].
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 300 # default is 900
    max_lifetime                 = 1800
  }]
  ```

  **`network_mode = "PUBLIC"`, and not only for simplicity.** The provider bug
  where `aws_bedrockagentcore_agent_runtime` leaves undeletable ENIs and hangs
  `destroy` on a dependency cycle is a **VPC-mode** problem — ENIs exist only
  there. This project destroys and rebuilds as a matter of routine, so `PUBLIC`
  removes that failure mode by construction instead of documenting a workaround
  for it. It is also correct on the merits: the agent reaches a public tunnel URL
  and public AWS APIs, and needs no VPC reachability.

  The default idle timeout is **900s**, and an idle session keeps accruing
  memory charges until it is reclaimed — exactly the idle-billing trap directive
  2 describes, left at three times the necessary size by simply not configuring
  it. Every other cost control in this plan is enforced in the harness or the
  workflow; this one is enforced by the platform and survives a harness bug.

  **Set both fields together.** Specifying `max_lifetime` alone triggers
  [hashicorp/terraform-provider-aws#45290](https://github.com/hashicorp/terraform-provider-aws/issues/45290):
  AWS computes a default for the omitted `idle_runtime_session_timeout`,
  Terraform expected null, and the apply fails with "inconsistent result after
  apply." Two lines, not one.

  **Implementation note on session length.** Synchronous invocations cap at
  **15 minutes**. That is under `max_lifetime` above, so any round expected to
  run longer must use the **async session API** — the harness should target it
  from the start rather than discovering the ceiling as a truncated run midway
  through Phase 3, where multi-round sessions are longest.

  Add a runbook
  note about the undeletable-ENI destroy hang before you ever need it.

**Slack.** **[corrected]** The first draft said "reuse the existing webhook" as
though it were free. It is in Secrets Manager under `pipelineguard/gates/dev`,
readable today only by the gate Lambda role. Pick one and write down which:
either add a `GetSecretValue` statement for that secret to the QA role (widens a
deliberately narrow role), or duplicate the webhook into vesselAI's Actions
secrets (a second copy to rotate). **Recommended: duplicate it.** Keeping the
gate role narrow is worth more than avoiding a second copy of a webhook URL.

### 1b. The QA agent

- **Tools:** Browser (cloud Chromium) and Code Interpreter. Nothing else. Every
  additional tool is more tokens *and* more session wall-clock per run.
- **The browser session must be ephemeral** — no profile persistence, no cookies
  or `localStorage` carried between sessions. Since **15 April 2026**, persisted
  browser profiles are stored in S3 and billed at **S3 Standard rates**, which is
  a separate meter the screenshot bucket's 7-day lifecycle rule does **not**
  reach: different bucket, different lifecycle, silently accumulating. Ephemeral
  is also the correct QA posture — every run should start from a clean browser
  against a freshly seeded database, or a stale cookie makes a finding
  irreproducible. Cost and correctness agree here; take both.
#### Two programs, not one **[corrected]**

An earlier revision said "harness location: `agents/qa/`, mirroring the existing
gate layout" and left it there. That conflated **two separate programs running in
two different places**, which is the most consequential architectural error the
plan has carried. AgentCore Runtime *hosts agent code*: you package an agent,
deploy it, and invoke it. It is not a model endpoint you call with a prompt.

| | Where it runs | What it is |
|---|---|---|
| **The agent** | Inside AgentCore Runtime, in AWS | Claude Agent SDK agent, Browser + Code Interpreter tools, the rubric as its system prompt. Packaged as a container image. |
| **The harness** | The GitHub runner | Client-side orchestrator: calls `InvokeAgentRuntime`, enforces budgets between turns, validates the JSON, posts the comment. Holds no rubric. |

- **Agent package:** `agents/qa/agent/` — zipped and uploaded to the code bucket,
  and pointed at by the runtime's `code_configuration`. This is where the rubric
  prompt and the tool configuration live. Python, to match the runtime enum and
  the rest of this repo's non-app code.
- **Harness:** `agents/qa/harness/` — Python, mirroring the existing gate layout
  (`handler.py` + focused modules + `tests/`). Add its test directory to
  `testpaths` in `pytest.ini` so the existing `pytest -q` CI job picks it up with
  no workflow change. The gate-layout convention applies to *this* half only.

**How the rubric reaches the runtime.** Decide deliberately and record it,
because it determines what a rubric change costs:

- **Baked into the package** (system prompt in the agent code) — one artifact,
  one version, but *every rubric tweak is a re-zip, re-upload and runtime
  update*. Cheaper than the image rebuild this once said, though still not free.
  Phase 1's exit criteria involve iterating on the rubric until the
  false-positive rate is acceptable, so the tuning loop still matters.
- **Passed per-invoke** — the harness sends the rubric with each call. Fast to
  iterate, but the rubric is then client-supplied, so the agent's behaviour is no
  longer pinned by the deployed artifact.

**Recommended: baked into the package, with a per-invoke override behind a
`workflow_dispatch` input.** Tune fast by hand, and let the committed image be
the source of truth for scheduled and PR runs. Same shape as the AI-key decision
in Phase 0.5 #4 — attended runs get the loose setting, automated ones get the
pinned one.
- **System prompt** carries an explicit QA rubric. This is the single
  highest-leverage artifact in the project — it is what separates real findings
  from noise. It must state:
  - What counts as a finding vs. an intentional design choice
  - The severity ladder, with concrete examples at each level
  - Report only what is **observed**, never what is inferred or
    expected-by-convention
  - A hard cap on pages, listed explicitly by route
  - **Known-by-design behaviours, enumerated.** vesselAI's README has a
    "What's Real vs. Mocked" section; every mocked surface listed there is a
    guaranteed false positive unless the rubric names it. Lift that list into the
    prompt verbatim and keep the two in sync.
- **Output schema** — strict JSON, validated before anything is posted:

```json
{
  "overall": "PASS | FAIL",
  "pages_tested": 0,
  "session_seconds": 0,
  "cost": {
    "model_tokens": { "input": 0, "output": 0, "estimated_usd": 0.0 },
    "runtime_session_usd": 0.0,
    "browser_session_usd": 0.0,
    "estimated_total_usd": 0.0,
    "excludes": ["S3 storage", "CloudWatch Logs", "GitHub runner minutes"]
  },
  "findings": [
    {
      "id": "F-001",
      "severity": "CRITICAL | HIGH | MEDIUM | LOW",
      "page": "/route",
      "summary": "one line",
      "evidence": "what was observed",
      "screenshot": { "key": "s3 key", "url": "presigned, expires with the object" },
      "steps_to_reproduce": ["..."],
      "expected": "...",
      "actual": "...",
      "suspected_source": "file or subsystem guess, may be null"
    }
  ]
}
```

  Validate against this schema. A response that does not parse is a **failed
  run**, not a set of findings. In the reference implementation, unparsed agent
  narration leaked into the findings table as HIGH-severity rows reading things
  like "Let me verify this further" — noise presented as bugs.

  `session_seconds` is new, and is there because of directive 2: without it the
  PR comment reports only half the cost.

  **[corrected]** The `cost` object replaces a flat
  `token_usage.estimated_usd`, which **understated the bill by construction**.
  Three separate meters run during a QA session and the old field named one:

  | Meter | What it bills |
  |---|---|
  | Model inference | Input/output tokens, priced per model |
  | AgentCore runtime | The session itself, on the vCPU/GB meter |
  | Browser + Code Interpreter | The *same* vCPU/GB meter, separately metered |

  A single `estimated_usd` next to a token count reads as "this run cost that
  much." It did not, and the gap widens exactly when the agent is slow — the case
  you most want visibility into, since browser memory bills on wall-clock
  including idle. Either sum the components under `estimated_total_usd`, as
  above, or state in the schema what the number excludes. This plan does both,
  because a cost field with an explicit `excludes` list is honest in a way a
  bare total is not — and this is a FinOps repo, where the reporting is the
  product.

  **The PR comment cost line must read the same way:** model tokens + runtime
  session + browser, then the total. Not tokens alone.

### 1c. Workflow wiring — `.github/workflows/ui-qa-agent.yml`, **in vesselAI**

Three triggers on one workflow. They compose; the cost controls in 1d are what
keep them from multiplying spend:

- `workflow_dispatch` — manual, with inputs for route set, model rung, and the
  real-vs-dummy AI key (Phase 0.5 #4)
- `schedule` — periodic regression sweep, reduced route set, cheap rung,
  **gated by the `QA_SCHEDULE_ENABLED` repo variable** (see below)
- `pull_request` — gated by the `agent-qa` label (1d)

**[added] A cron has no label.** The `agent-qa` gate is described in 1d as the
single most effective cost control precisely because it makes every run a
deliberate choice — but that argument does not extend to `schedule`, which is
recurring spend forever, decided once, by someone who has since stopped thinking
about it. It is the only trigger here that can quietly bill while nobody is
looking at the repo.

So give it a documented kill switch: a repo variable `QA_SCHEDULE_ENABLED`,
checked as `if: github.event_name != 'schedule' || vars.QA_SCHEDULE_ENABLED == 'true'`.
Default it **off**, so the sweep is switched on as a conscious act, and name it
in the runbook next to the spend cap. Turning the regression sweep off for a
month should be one click and require no code change — which also means it can
be turned off in a hurry.

```
permissions:            # least privilege at the GitHub layer — nothing more
  id-token: write       # mint the OIDC token for the AgentCore role
  contents: read        # checkout
  pull-requests: write  # post the findings comment

concurrency:
  group: ui-qa-agent-${{ github.event_name }}-${{ github.ref }}
  cancel-in-progress: true
```

**[added]** The `permissions:` block was missing from an earlier revision of this
spec, which left the least-privilege story complete on the AWS side and silent on
the GitHub side. Declare it at workflow level so it applies to every job; the
default is otherwise whatever the repo setting says, which is usually more than
this needs. `pull-requests: write` is required for the findings comment and is
the only write scope here — note that it does **not** grant push access, which
Phase 2's fix agent will need separately and should obtain via its own job-scoped
grant rather than by widening this one.

**Pin every action to a commit SHA**, not a tag. `actions/checkout@<sha>`, not
`@v4`. Tags are mutable; a compromised or retagged action runs inside a job
holding `id-token: write`, which is the one credential in this design that
reaches AWS. Renovate or Dependabot can keep the SHAs current with the version in
a trailing comment.

**[revised]** Under the EKS design this was a global group with
`cancel-in-progress: false`, because cancelling mid-apply stranded a cluster.
With an in-runner target there is nothing to strand, so this reverts to the
conventional per-ref setting — matching PipelineGuard's own `ci.yml`. A
superseded run is cancelled and its stack dies with the runner.

**`github.event_name` is in the key deliberately. [corrected]** Keying on
`github.ref` alone collides `schedule` and `workflow_dispatch`, which both
present `refs/heads/main` — so triggering a manual run would cancel a regression
sweep mid-session. That is not merely inconvenient: with `cancel-in-progress:
true` the killed run has already spent its AgentCore session and gets nothing for
it, so a group-key bug is a directive-2 cost bug. PR runs are unaffected either
way (`refs/pull/N/merge` is distinct per PR), which is exactly why this is easy
to miss in testing — the trigger you exercise most is the one that never
collides.

Steps:

1. **Assume the AgentCore role via OIDC** — PipelineGuard's account only. One
   role, short-lived, no long-lived keys. vesselAI's AWS account is not touched.
   **This job carries the fork guard** from Phase 1a
   (`github.event.pull_request.head.repo.fork == false`); nothing downstream
   runs without it, because nothing downstream has credentials.
2. **Bring the stack up** — `docker compose -f docker-compose.prod.yml up -d
   --build --wait`, with:
   - `POSTGRES_PASSWORD` and `JWT_SECRET` **generated per run** (both are
     required-with-no-default in the compose file; both are ephemeral, so
     generate rather than store them — `JWT_SECRET` must be ≥32 chars)
   - `ANTHROPIC_API_KEY` from Actions secrets (Phase 0.5 #4)
   - Buildx layer caching against the GitHub Actions cache — the image build,
     not the app start, is the bulk of the 3–5 minutes
   - `--wait` blocks on the existing healthchecks, so `migrate` and `seed` are
     guaranteed complete before the next step
3. **Open the tunnel** to `:8080`, capture the public URL from its output, and
   register a trap so the tunnel is torn down with the job. This is the only
   externally-reachable surface, and it exists for minutes.
4. **Health assertion gate.** Against the *tunnel URL*, not localhost — the
   agent will use the tunnel, so the gate must prove the same path works:
   - The tunnel host resolves and returns 200 for `/`
   - `GET /api/analytics/health` (open, no auth) returns **JSON, not HTML** —
     this also proves nginx's longest-prefix routing reaches the FastAPI service
     rather than falling through to the SPA
   - `POST /api/auth/login` with the demo credentials returns a usable JWT —
     the real proof that `seed` ran
   - `GET /api/fleet` with that JWT returns fleet data — an authenticated,
     Postgres-backed read, so the whole chain is proven end to end
   - The served bundle contains no `undefined` in its API configuration

   If this fails, **stop and report a pipeline error. Do not invoke the agent.**
   This is the guard against burning an agent round — and a browser session's
   wall-clock — on the pipeline's own bug.
5. **Invoke the QA harness** against the runtime in PipelineGuard's account,
   passing the tunnel URL as the target.
6. **Validate JSON**, post findings as a PR comment (reuse
   `github_commenter.py`), embedding the presigned screenshot URLs the agent
   returned. **[revised]** Archiving to S3 and publishing metrics happen inside
   the runtime's execution role, not here — see 1a. This step performs no AWS
   writes, which is what keeps the CI-reachable role at two statements.
7. **Slack notification.**
8. **Nothing to tear down.** `docker compose down -v` in `always()` is good
   hygiene for a self-hosted runner, but on GitHub-hosted runners the machine is
   destroyed regardless. Compare with the EKS design, which needed an ordered
   finalizer teardown *plus* an out-of-band reaper to reach the same guarantee.

### 1d. Cost controls (build these now, not later)

- **Label gate.** Only run on PRs carrying `agent-qa`. Still the single most
  effective control available *for PR runs*: it makes every one a deliberate
  choice.
- **`QA_SCHEDULE_ENABLED` kill switch** *(new)* — the cron's equivalent, because
  a schedule carries no label and is the one trigger that bills unattended.
  Default off. See 1c.
- **Runtime `lifecycle_configuration`, enforced by the platform** *(new)* —
  `idle_runtime_session_timeout = 300` (down from the 900s default) and
  `max_lifetime = 1800`. Unlike every other control in this list, this one is not
  harness logic and therefore survives a harness bug or a hung session. Set both
  fields together; see 1a for the provider bug that bites if you don't.
- **Ephemeral browser sessions** *(new)* — no profile persistence. Persisted
  profiles bill S3 Standard on a meter the screenshot lifecycle rule does not
  cover. See 1b.
- **`paths:` filter** so the agent never runs on README- or docs-only PRs. Scope
  it to `frontend/**` and `backend/**` — changes under `data-platform/`,
  `k8s/`, `terraform/` and `frontend-angular/` cannot affect the React UI
  under test.
- **Per-run token budget.** **[corrected]** The first draft said "abort the
  session when exceeded," which is not implementable — you cannot preempt a model
  mid-generation from outside. What is: cap `max_tokens` per call, cap harness
  loop turns, and check cumulative usage *between* turns, stopping at the next
  turn boundary and reporting a partial run.
- **Wall-clock session cap** *(new, directive 2)*. Hard timeout on the browser
  session, enforced in the harness, well below the job timeout.
- **Job-level `timeout-minutes`** *(new)* — the outer backstop, sized to cover a
  ~5 min stack build plus the session cap, and no more. **[revised]** This was
  ~20 min of provisioning under the EKS design; a tight job timeout is now
  affordable, which makes it a real control rather than a formality.
- **Explore-cap in the prompt** — a fixed list of routes, no open-ended crawling.
- **Screenshot discipline** — capture only on finding, never on every navigation.
  Screenshots are the largest single contributor to input tokens.
- **Log token usage *and* session seconds** to CloudWatch as custom metrics, and
  put both in the PR comment. Cost visible on every PR beats a monthly surprise.
- **Report all three meters, not just tokens** *(new)* — model inference,
  runtime session, and browser/code-interpreter bill separately. A tokens-only
  figure understates the run, and understates it most when the agent is slow.
  See the `cost` object in 1b.
- **Report runner minutes too** *(new)* — in the same comment. They are free on a
  public repo, so the honest line is "runner minutes: N (billed: $0.00)". This is
  a FinOps repo; reporting a zero you can defend is worth more than omitting the
  row. If the repo ever goes private, the row is already there and starts
  costing.
- **Cheapest model that clears the quality bar — tested, not assumed.** The
  reference implementation ran one round on a weaker rung and got findings
  stronger models never reproduced; they were agent noise. False positives cost
  human review time, which is the expensive resource. Benchmark two rungs on the
  same PR and record the difference.

### 1e. Target lifecycle *(revised — this section used to be "Teardown and the reaper")*

Under the EKS design this section specified three teardown layers — an ordered
`make destroy` in `always()`, an orphan check posted to the PR, and a scheduled
reaper sweeping clusters by TTL tag — because at $0.37/hr one layer was not
enough and the runner dying mid-job could strand real money.

**All three are gone.** The target lives and dies inside the job: the stack, its
volume, and the tunnel are all destroyed when the runner is. That is the single
largest simplification in this revision, and it is worth stating plainly in an
interview — *the cheapest teardown is the one you do not have to write.*

What actually remains:

- **`docker compose down -v` in `always()`.** Hygiene, not a guarantee. It
  matters only if this ever runs on a self-hosted runner, where the machine
  survives the job. Write it now so that migration is not a landmine.
- **Bound the tunnel to the job.** Trap on exit. A quick tunnel is an
  unauthenticated public URL; there is no reason for it to outlive the run, and
  no reason to write it anywhere durable — not into the PR comment, not into the
  findings JSON, not into logs. Screenshot keys go to S3; the URL does not.
- **The EKS path stays manual.** When you do want deployment-path fidelity, run
  it by hand from a laptop with `make apply` / `make destroy`, exactly as the
  Makefile intends. It never enters CI, so no remote state is needed and its
  local-state rationale stays true.

**Exit criteria:** a PR gets a trustworthy, schema-valid findings comment
carrying token cost, session seconds, and runner minutes. The stack comes up
clean from a cold cache in under ~5 minutes, and the health gate correctly
*refuses* to invoke the agent when `seed` has been deliberately broken.
False-positive rate is low enough that the report is worth reading.

> **On "run it on 3 real PRs" [corrected].** vesselAI is actively developed, so
> real PRs exist — use them. But add a deliberate corpus: reintroduce two or
> three of the contract-drift bugs the README's audit describes (a reshaped API
> response, a `camelCase`/`snake_case` mismatch, an enum case mismatch) on a
> branch and measure whether the agent finds them. Real PRs give you the
> false-positive rate; seeded bugs give you the **false-negative** rate, which no
> amount of watching real PRs will tell you. Report both.

---

## Phase 2 — Bug-fix agent (proposes, never loops)

Second harness. Input: the findings JSON plus vesselAI's source. Output: a PR
against vesselAI. Because the workflow already lives in that repo, this needs no
cross-repo token — that is the main reason for the Phase 0 repo-layout decision.

### Mechanism **[added]**

An earlier revision said "second harness" and left the mechanism unstated,
which invited the assumption that it mirrors Phase 1 — a second AgentCore
runtime, a second image, a second session. **It should not.** Ask what this agent
actually does: read findings JSON and a bounded set of source files, emit
`{file, old_string, new_string}` objects. That is text in, text out.

- **No Browser.** It never looks at the app; Phase 1 already did that and wrote
  down what it saw. Carrying the Browser tool here would bill the vCPU/GB meter
  for a capability that is never invoked.
- **No Code Interpreter.** The compile-and-test gate runs `tsc` and Vitest **in
  the job**, against the real repo, with the real toolchain — which is stronger
  than anything the agent could run against an uploaded copy, and is the gate
  that actually blocks the PR.
- **Therefore no second runtime.** A direct `bedrock:InvokeModel` from the
  harness in the runner is sufficient and is the better choice: no image to
  build, no ARM64 build script, no runtime lifecycle to configure, no session
  billing, and no artifact-upload problem at all.

**How the source reaches it — it doesn't.** This is the part the omission was
hiding. vesselAI is a six-module monorepo with two frontends, a backend, and a
data platform; uploading it to a runtime workspace is both impractical against
payload limits and unnecessary. Instead the **harness selects** a bounded file
set in the runner, where the repo is already checked out:

1. Start from each finding's `suspected_source` (Phase 1's schema emits it,
   nullable, precisely for this).
2. Fall back to a grep seeded by the finding's `page` route and `summary` when
   it is null.
3. Cap the result — max files and max total bytes — and record what was excluded
   in the PR summary, the same way skipped patches are recorded.

A finding whose source cannot be located within the cap is reported as
**skipped, with the reason**, not guessed at. The reference implementation's
"could not locate source; skipped" rows were a symptom of exactly this step being
implicit; making it explicit turns them from a failure into an honest output.

**Consequence for Phase 1a:** the QA agent's execution role needs
`bedrock:InvokeModel` on the QA model ARN; the *harness* now needs it too, for
the fix model. Scope both to specific model ARNs, and note that they may differ —
a cheaper rung is often fine for QA triage but not for code edits.

- **Structured edits, not raw diffs.** Have the agent return
  `{file, old_string, new_string}` objects and apply them programmatically.
  Freeform unified diffs fail to apply constantly — the reference run is littered
  with findings marked "could not locate source; skipped" and "agent produced no
  applicable diff."
- **Deny-list, enforced in code before applying anything.** **[corrected]** The
  first draft's list was too narrow for this repo pair. Full list:
  - `**/*.tf`, `**/*.tfvars`, `terraform/**`, `infra/**`, `**/backend.conf`
  - `.github/**` — the whole directory, not just `workflows/`
  - `buildspecs/**` — CodeBuild YAMLs are pipeline definitions, exactly as
    sensitive as workflows, and the first draft missed them entirely
  - `k8s/**` — manifests, Kustomize overlays, sealed secrets, Argo CD Applications
  - `scripts/**` and `Makefile` — deploy and **teardown** paths; an agent editing
    `make destroy` is an agent editing the thing that stops the billing
  - `gates/**` and `agents/**` — the agent's own guardrails
  - `**/secrets*`, `**/*.pem`, `**/*.key`, `.env*`
  - any IAM policy document
  - plus **max-files-touched** and **max-lines-changed** thresholds
- **Allow-list as the real control.** A deny-list fails open on anything nobody
  anticipated. Since the target is one React frontend, invert it: permit edits
  **only** under `frontend/src/**` and `backend/src/**`, and treat the deny-list
  as defence in depth. Reject anything outside, loudly, in the PR summary.
- **Compile and test before committing.** Run type-check and the full Vitest
  suite in the job. Reject any patch that does not build. The reference
  implementation shipped an auto-fix reading a field never added to the
  TypeScript interface — a latent build break that surfaced rounds later.
  vesselAI is TypeScript strict mode with tests at every layer, so this gate has
  real teeth here.
- Push to `agent-fix/pr-<n>`, open a PR, post a summary table of what was
  patched, what was skipped, and why.
- **Do not open that PR with `GITHUB_TOKEN`. [corrected]** Events generated by
  `GITHUB_TOKEN` deliberately do not trigger further workflow runs — GitHub's
  guard against recursive CI. So a fix PR opened with it arrives with **zero
  checks**, and Phase 0.5 #1 requires checks to pass before merge. The result is
  a PR that is permanently unmergeable until a human opens it and manually
  re-runs the suite: an "automated" fix loop whose every output needs a manual
  nudge before it can even be evaluated.

  This is *not* fixed by widening the workflow's `permissions:`. Note the
  interaction with Phase 1c's least-privilege block — `pull-requests: write` does
  not grant push, but adding `contents: write` still would not help, because the
  restriction is about the token's *identity*, not its scopes. The fix agent
  needs a **different identity**:

  - **A GitHub App installation token — recommended.** Scoped to vesselAI,
    `contents: write` + `pull-requests: write`, minted per run and expiring in an
    hour. Not bound to a person, so it survives you rotating your own
    credentials or leaving the project.
  - **A fine-grained PAT** — simpler to set up, same two scopes, same repo. But
    it is user-bound and long-lived, which is the pattern vesselAI's own
    `github-oidc.tf` argues against at length for AWS. Acceptable as a first
    step; note it as debt rather than pretending it is the end state.

- **Stop there.** One pass, one PR, no re-trigger. Branch protection provides the
  human gate.

**Exit criteria:** the agent opens a PR whose patches compile and pass tests, and
a human can review it in under ten minutes. Confirm by test that a patch
targeting a denied path is rejected rather than applied. **And explicitly: the
agent's own PR must show CI checks running on itself, with no human
intervention** — that is the criterion that catches the `GITHUB_TOKEN` trap
above, and it fails silently rather than loudly, so it has to be checked for
rather than assumed.

---

## Phase 3 — The convergence loop (only if Phases 1–2 are solid)

**Goal-driven, not round-counted.** This is the correction the reference
implementation had to make after its first design burned three rounds without
converging.

After each fix commit, re-run QA and compare the **finding set** against the
previous round:

- Finding set shrinking → continue
- Finding set identical or growing → **stall. Stop and hand to a human.**
- Zero blocking findings → done, report PASS

Backstops on top of the convergence check:
- Hard max rounds (3)
- Cumulative token budget across all rounds — abort on breach
- Cumulative wall-clock budget across all rounds
- Per-round wall-clock timeout

**Keep the stack up across rounds.** **[revised]** Under the EKS design this was
load-bearing — reprovisioning per round meant ~20 extra minutes and a fresh
teardown risk each time, which made the reaper essential. With compose it is a
minor optimisation: keep the containers running between rounds to skip the
rebuild, and accept that a round which changes backend source needs a rebuild
anyway. Nothing depends on getting this right.

There is also a subtlety worth naming: **rounds after the first run against a
database the previous round already touched.** If a fix round's QA pass writes
data, round N+1 is not testing a clean seed. Either restart the stack between
rounds (costs the rebuild) or restrict the rubric to read-only exploration.
Decide explicitly rather than discovering it as a flaky finding.

Post a reconciliation table on the final round: a per-finding ledger of what was
fixed, what was by-design, and what was agent noise.

**Cost warning:** this phase multiplies spend by the round count. **[revised]**
Under the EKS design each round also held a $0.37/hr cluster open, which made
this the most dangerous phase in the plan; now rounds cost tokens and browser
wall-clock only, which is the same shape as the original draft's warning. Still
do not enable it by default — gate it behind an explicit label separate from
Phase 1's.

---

## Best practices summary

**Security**
- OIDC only, no long-lived AWS keys anywhere
- `sub` pinned with `StringEquals`, never `StringLike` with a wildcard
- **An explicit fork guard on the credentialed job** — the `sub` claim is
  identical for fork and same-repo PRs and cannot do this job. Do not rely on
  GitHub's fork permission cap: it is a platform default, not your control
- `job_workflow_ref` pinned for `schedule` / `workflow_dispatch`; verified, not
  assumed, for `pull_request` (its ref is the merge ref)
- `pull_request`, never `pull_request_target`
- Workflow-level `permissions:` — `id-token: write`, `contents: read`,
  `pull-requests: write`, nothing more
- Actions pinned to commit SHAs, never mutable tags — the job holds the one
  credential that reaches AWS
- Least-privilege execution role, every statement scoped to a specific ARN
- **The CI-reachable role performs no AWS writes** — archiving and metrics live
  in the runtime's execution role, so the role a public repo can assume stays at
  invoke-one-runtime, read-one-secret
- Presigned screenshot URLs expire with the object; the public-repo exposure that
  implies is recorded as a decision, not left implicit
- Credentials from Secrets Manager at runtime, never in workflow YAML
- **No live billable credential behind the public tunnel** — dummy AI key by
  default, real key only on attended `workflow_dispatch`, spend cap regardless
- Agent never writes to infrastructure, workflows, buildspecs, k8s, or scripts —
  allow-list first, deny-list as backup
- Protected `main` on both repos; agent commits reach it only via human review

**Cost**
- Label-gated invocation for PRs; `QA_SCHEDULE_ENABLED` kill switch for the
  cron, default off — a schedule has no label and bills unattended
- Concurrency key includes `event_name`, so a manual run cannot cancel — and
  waste — a scheduled session already in flight
- **No AgentCore runtime for the fix agent** — it needs no browser and no
  session, so it is a direct model call, not a second hosted agent
- `paths:` filter scoped to the directories that can affect the UI under test
- **An ephemeral in-runner target, so there is no cloud resource to strand** —
  the largest cost control in the design, and the one that required no code
- **Runtime `lifecycle_configuration` set explicitly** — idle timeout 300s not
  the 900s default; platform-enforced, so it survives a harness bug
- **Ephemeral browser sessions** — persisted profiles bill S3 Standard on a
  meter the screenshot lifecycle rule never reaches
- Token budgets *and* wall-clock caps, enforced not advisory
- All three meters in the PR comment — model tokens, runtime session, browser —
  plus session seconds and runner minutes; never a tokens-only total
- Tight job `timeout-minutes`, affordable because provisioning is minutes not
  tens of minutes
- 7-day S3 lifecycle on screenshots; capture only on finding
- Route allow-list; no open-ended crawling
- Model rung chosen by benchmark, counting false-positive review time as cost

**Reliability**
- The agent and the harness are separate programs — agent in the runtime image,
  budgets and validation client-side; never conflate them
- Agent shipped as an S3 code artifact, not a container — no image, no
  architecture to get wrong
- Agent code in its own bucket, never one carrying an expiry rule
- Runtime `network_mode = "PUBLIC"` — the destroy-hang provider bug is VPC-only
- Provider lower-bounded at the version that actually has the resource
- Agent PRs opened with an identity whose events trigger CI, not `GITHUB_TOKEN`
- Health-assert the target *through the tunnel* — 200, JSON-not-HTML, a real
  login, an authenticated DB-backed read — before invoking any agent
- Lean on the compose healthchecks (`--wait`) rather than reimplementing them
- Strict JSON schema validation; unparsed output = failed run
- Structured edits over diffs
- Compile + full test suite before any agent commit
- Convergence check, not a round counter
- A fresh seeded database per run, so findings are reproducible

**Observability**
- Token usage, session seconds, and runner minutes as CloudWatch custom metrics
- Per-meter cost in the findings JSON with an explicit `excludes` list — a total
  you can defend beats a total that looks tidy
- Findings JSON archived to S3 per PR
- Slack notification reusing the existing PipelineGuard webhook

---

## Suggested commit sequence

Phase 0.5 comes first and is not optional. Cost controls land *with* the harness,
not after it — directive 1 says they are built in, not bolted on, and the
original draft's ordering contradicted that by putting budget enforcement a
commit later than the workflow that spends the money.

**[revised]** Four commits shorter than the EKS version — the two vesselAI
Terraform/IAM prerequisites and the reaper workflow are all gone, and vesselAI
now receives exactly one new file.

**Phase 0 / 0.5 — prerequisites**
1. `docs(agentcore): discovery notes on both repos` — this plan + `DISCOVERY.md`
2. `chore(infra): upgrade aws provider to ~> 6.0` *(PipelineGuard)*
3. — *branch protection on both repos; enable Bedrock model access; add the
   `ANTHROPIC_API_KEY` Actions secret to vesselAI* —

**Phase 1 — QA agent**
4. `feat(infra): S3 + Secrets + IAM execution role for QA agent` *(PipelineGuard)*
5. `feat(infra): GitHub OIDC role scoped to the vesselAI workflow` *(PipelineGuard)*
6. `feat(agent): QA agent package — rubric prompt, Browser + Code Interpreter` *(PipelineGuard)*
7. `feat(infra): code bucket + packaging script for the agent zip` *(PipelineGuard)*
8. `feat(infra): AgentCore runtime — code artifact, PUBLIC, lifecycle caps` *(PipelineGuard)*
9. `feat(agent): QA harness — invoke, budget enforcement, schema validation` *(PipelineGuard)*
10. `feat(ci): ui-qa workflow — compose target, tunnel, health gate` *(vesselAI)*
11. `feat(obs): per-meter cost reporting` *(both)*
12. — *ship, run on real PRs and the seeded-bug corpus, tune the rubric* —

**[revised]** Commit 7 was `ECR repo + arm64 build script`. With
`code_configuration` there is no image, so it becomes a bucket and a zip-and-
upload script — smaller, and it drops the ordering constraint that made the old
version awkward (a runtime cannot reference an image that does not exist, so the
ECR path forced a `-target`ed 3-phase apply; an S3 object has the same
requirement but uploading a zip is one command, not a Docker build).

**[revised]** Commits 6–9 were previously one commit, "QA harness with rubric
prompt, JSON schema, and budgets." That was the A1 conflation in schedule form:
the agent and the harness are separate programs in separate places, and the
agent artifact must exist in S3 before the runtime that references it can be
created — so the ordering is package, upload, then apply the runtime.

**Phase 2 — bug-fix agent**
13. — *set up the GitHub App (or fine-grained PAT) for vesselAI* —
14. `feat(agent): bug-fix harness with path allow-list and structured edits`
15. `feat(ci): compile-and-test gate before agent commits`
16. — *ship, review real agent PRs — confirm CI runs on the agent's own PR* —

**Phase 3 — convergence**
17. `feat(ci): convergence-based retry loop behind a separate label gate`

# Security audit fixes — items 3–5

## 3. Invocation-specific Terraform extraction

The direct security-gate handler downloads and extracts its archive inside a
fresh temporary directory. Evaluation stays inside that directory's lifetime,
and cleanup runs on both success and failure. Consecutive dummy archives prove
that removed Terraform files cannot survive into the next scan and that a
README-only artifact fails the existing nonempty-Terraform guard.

## 4. Browser request policy

The trusted runtime installs a CDP request guard before model-driven navigation.
It allows same-origin GET, HEAD and OPTIONS requests and exactly one POST to
`/api/auth/login`, with a JSON body containing only the configured email and
password. Other HTTP writes and external-origin requests are blocked. Document
navigations and explicit navigate calls use the rubric's route list. Ordinary
clicks and display controls remain available behind the request guard.

Service-worker bypass and WebSocket URL blocking are configured. Secondary
targets are auto-attached with execution suspended and closed without resuming.
Unsupported guard commands abort setup; failures while handling a paused
request stop further tool execution. Teardown closes the browser before
disconnecting interception, retaining the connection for SDK teardown when
that command is unavailable.

The local dummy fixture grants the QA principal write access and the content
author read access. An unguarded QA PATCH changes a protected record; the guarded
PATCH does not reach the dummy application. Login and a chart read remain
allowed. Mocked websocket tests exercise interception events, session routing,
secondary targets, setup failure and teardown. No real model, remote browser,
vesselAI application or production identity was tested. This establishes local
handler behavior, not model-causal exploitation or deployed-browser compatibility.

This is HTTP defense in depth, not a server authorization boundary. A server
that writes on GET or uses another transport still requires a QA principal
denied mutation privileges. Those vesselAI changes are outside this repository.
Cross-origin dependencies, workers and popup-based views are deliberately
excluded and may reduce coverage. SPA history changes without a document
request are not a complete navigation boundary. Before rollout, validate CDP
compatibility and these coverage limits in a separately authorized disposable
environment; never infer successful deployment from these unit tests.

No new runtime module was added. After merge and pull into the main checkout,
follow the existing package, code-version pin and deployment procedure. No
packaging, production invocation or infrastructure deployment was performed for this PR.

## 5. Pipeline IAM separation

Each CodeBuild stage has a separate role. The existing role is retained for
Test, with only artifact/log access and the necessary KMS encryption operations.
Its inline policy moves to the `test` instance without replacing that role.
Build can push only the app's ECR repository and read its SSM URL. Each gate
can invoke only its own Lambda. Trust policies bind each role to its matching
CodeBuild project and account.

Plan and Deploy can read the exact configured layer1 state object, read layer2
state and manage only its exact lock object. Deploy alone can write the layer2
state object. Bucket listing supports backend workspace discovery but grants
no additional object reads or writes. No stage can administer the persistent
CMK or its aliases. Deploy's service updates, task registration/deregistration
and tagging are scoped to the app; PassRole is restricted to its two ECS roles
and the `ecs-tasks.amazonaws.com` service. The only wildcard resources are ECR
authorization, availability-zone discovery and DescribeTaskDefinition, whose
AWS action tables provide no resource type. See the current
[ECS authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html)
and [S3 backend permissions](https://developer.hashicorp.com/terraform/language/backend/s3#permissions-required).

The Terraform regression uses a mock AWS provider and dummy account/resources;
its virtual apply never contacts AWS. It checks the rendered IAM documents and
project attachments for denied pre-gate deployment, denied layer1 state writes,
exact layer2 writes/locks, scoped ECR/ECS writes, task-role passing and retained
encrypted artifact/remote-state access. It runs in CI with Terraform 1.15.8.
These assertions are local policy evidence, not an observation of live IAM,
bucket policies, permission boundaries or organization SCPs.
Negative controls in the scratch copy deliberately added ECS authority to Test
and redirected Deploy's state write to layer1; both failed the assertions.

Infracost authentication was restored and the organization preflight found no
configured policies, guardrails or budgets. A repository scan completed with
no critical/warning diagnostics or policy failures. New IAM roles/policies are
free resources; the scan's configured two-layer estimate was $74.71/month,
including the $73.31 ephemeral stack when provisioned. This is a configuration
estimate, not current billing; AgentCore runtime pricing is unsupported by the
scanner. No infrastructure was provisioned for verification.

Before rollout, inspect a real layer2 plan under separate deployment
authorization. Persistent layer1 state, runtime and code-version pins were not
changed. Terraform jobs assume an existing default-workspace layer2 state:
initial stack provisioning, IAM/infrastructure changes and state recovery belong
to an operator, not these constrained pipeline roles.

The toolchain mismatch is resolved: Plan, Deploy and CI now pin Terraform
1.15.8, and both roots require `>= 1.10, < 2.0` to support `use_lockfile`.
S3 native locking was added in [Terraform 1.10](https://github.com/hashicorp/terraform/blob/v1.10.0/CHANGELOG.md).
The mock regression intentionally bypasses the backend and therefore does not
validate backend initialization or a live deploy.

The current single-operator/main-admitted-code trust model makes this IAM work
hardening; no lower-trust contributor exposure has been established.

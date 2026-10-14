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
packaging, production invocation or Terraform apply was performed for this PR.

## 5. Pipeline IAM separation — pending

No Terraform changes have been generated. The required Infracost organization
policy, guardrail and budget preflight is blocked by an expired saved login.
After authentication is restored, separate stage roles; keep Test free of ECS
deployment authority, restrict layer1 state to reads and layer2 state/locks to
exact keys, remove CMK administration, and scope supported ECR/ECS operations.
Verify deny/allow cases locally and inspect a plan before any separately
authorized apply. Persistent layer1 state and runtime pins must be preserved.

The current single-operator/main-admitted-code trust model makes this IAM work
hardening; no lower-trust contributor exposure has been established.

# AgentCore UI-QA agent — diagram guide

Source: [`agentcore-qa-agent.drawio`](agentcore-qa-agent.drawio) (editable; the exported
`agentcore-qa-agent.drawio.png` has the XML embedded, so the PNG opens in draw.io too).

Scope: the **layer1_persistent** QA core only — the always-on half of PipelineGuard
(`aws_kms_key.main` + `module.qa_agent`), account `149751500899`, `ap-southeast-1`. The demo
pipeline (layer2_ephemeral) is a separate root with its own state and is not shown; the two
share only the KMS key.

## Flow

1. A workflow in **amdhd/vesselAI** brings the app under test up from its own
   `docker-compose.prod.yml` inside the GitHub runner and exposes it through an ephemeral tunnel.
2. The workflow assumes the **GitHub OIDC role** in this account. That role carries two
   statements: invoke one runtime, read one secret.
3. It calls `InvokeAgentRuntime` on the **Bedrock AgentCore runtime**. The runtime is PUBLIC-mode
   (no VPC, no ENI, no NAT) and bills per session, never while idle.
4. The runtime runs the agent — rubric, deterministic candidate layer, browser driving — and calls
   **Bedrock** for the model turns (Claude Sonnet 4.6 by default; Haiku 4.5 is an explicit opt-in).
5. It drives a cloud Chromium through **AgentCore Browser** over CDP, which reaches the tunnelled
   app over HTTPS.
6. The runtime writes its own artifacts under its execution role: findings JSON and screenshots to
   the reports bucket, logs and run metrics to CloudWatch, QA target credentials read from Secrets
   Manager. Its code comes from the separate versioned code bucket, pinned by S3 object version id.
7. Back on the runner, the harness re-validates the findings JSON, prices the run and posts the PR
   comment. It holds no rubric and makes no model calls of its own.

## Services

| Node | Purpose |
|---|---|
| GitHub Actions runner | QA harness (invoke · validate · price · comment) and the opt-in fix harness |
| IAM role (GitHub OIDC) | The only CI-reachable identity; two statements, no S3 access |
| Bedrock AgentCore Runtime | Where the agent runs; session-billed, PUBLIC mode, session lifetime capped in Terraform |
| Bedrock | Model calls, via inference profiles (current-gen Anthropic models are profile-only) |
| AgentCore Browser | Managed Chromium the agent drives over CDP |
| S3 — reports | `findings.json` + screenshots, 7-day expiry (which also bounds presigned links) |
| S3 — agent code | The deployment zip; versioned, never expires, pinned by version id |
| Secrets Manager | QA target credentials — Terraform owns the container, never the material |
| KMS | The layer1 CMK; encrypts both buckets and the secret |
| CloudWatch | Runtime logs (retention set explicitly — AgentCore creates the group with none) and run metrics |

## Design decisions the diagram encodes

- **The runtime writes its own reports.** That is what keeps the OIDC role a public repo's CI can
  assume down to two statements — no S3 permission is handed to CI at all.
- **Two buckets, not one.** The reports bucket expires everything after 7 days; agent code parked
  there would vanish a week after it started working. A separate bucket makes that impossible by
  construction.
- **PUBLIC mode is deliberate.** The agent reaches a public tunnel URL and public AWS APIs, and
  needs no VPC reachability — which also avoids the VPC-only provider bug that leaves undeletable
  ENIs behind on destroy.

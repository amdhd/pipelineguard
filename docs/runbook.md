# PipelineGuard — Runbook

## First-time deploy

The stack is TWO Terraform roots with separate states (see infra/README.md):
**layer1_persistent** (QA core, always up) and **layer2_ephemeral** (demo stack,
destroyed between demos). First-time bring-up:

```bash
# 1. Bootstrap remote state (creates S3 bucket, writes both layers' backend.conf)
AWS_PROFILE=your-profile ./scripts/bootstrap.sh dev ap-southeast-1

# 2. QA core — layer1. Applied once and left up (~$1.40/mo). Carries the
#    AgentCore QA runtime; the vesselAI QA workflow assumes its roles.
./scripts/apply-dev.sh -auto-approve

# 3. Demo layer — layer2. demo-up.sh does the cold-start two-phase apply
#    (security-gate ECR repo → deploy-gates image → rest of the stack → app
#    image) and prints the ALB URL.
./scripts/demo-up.sh -auto-approve

# 4. Seed the gate API keys straight into Secrets Manager.
#    They are NOT Terraform variables: `terraform show -json` writes sensitive
#    values into plan.json in plaintext, and plan.json ships as a pipeline
#    artifact to S3. Keeping them out of Terraform keeps them out of the plan.
export INFRACOST_API_KEY="ico-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export GITHUB_TOKEN="ghp_..."          # optional; enables the PR comment
./scripts/seed-gate-secrets.sh dev ap-southeast-1

# 5. Authorise the GitHub connection ONCE in the console:
#    Developer Tools -> Settings -> Connections -> pg-dev -> Update pending connection
#    (Terraform creates it in PENDING state — this handshake cannot be automated.)
```

## Common operations

| Task | Command |
|---|---|
| Bring up the demo layer | `./scripts/demo-up.sh` |
| Tear down the demo layer (leaves QA core up) | `./scripts/demo-down.sh` |
| Re-run the pipeline | Push to `main`, or "Release change" in the CodePipeline console |
| Change cost threshold | Edit `cost_gate_threshold` in `infra/layer2_ephemeral/dev.tfvars`, `terraform -chdir=infra/layer2_ephemeral apply -var-file=dev.tfvars` |
| Rebuild a gate layer | `./scripts/deploy-gates.sh` then finish the layer2 apply |
| Tail gate logs | `aws logs tail /aws/lambda/pipelineguard-security-gate-dev --follow` |
| Tail app logs | `aws logs tail /ecs/pipelineguard-dev --follow` |
| Local scan | `./scripts/local-scan.sh` |

## When the cost gate blocks

1. Read the Slack message / CodeBuild log for the `$X/month` delta and top drivers.
2. Right-size the offending resource (often the NAT GW, ALB, or task size).
3. If the increase is intentional, raise `cost_gate_threshold` and re-apply.

## When the security gate blocks

1. Open the PR comment — Claude's report lists each HIGH/CRITICAL finding + remediation.
2. Container CVEs: bump the base image / patch the vulnerable package, rebuild.
3. IaC findings: fix the Checkov violation in Terraform.
4. Re-push. The gate re-runs automatically.

## When a deploy fails

- The ECS circuit breaker rolls back to the last healthy task set automatically.
- Confirm rollback: `aws ecs describe-services --cluster pipelineguard-dev --services pipelineguard-app-dev`.
- Inspect task stop reasons in the ECS console → Tasks → Stopped.

## Teardown

Between demos, destroy only the demo layer — this never touches the QA core:

```bash
./scripts/demo-down.sh -auto-approve
```

To stop the WHOLE project (QA core included — the AgentCore runtime the vesselAI
QA workflow invokes goes away until recreated):

```bash
./scripts/destroy-dev.sh -auto-approve
```

Both empty the ECR repos first (Terraform can't delete non-empty repos). The
remote-state bucket is NOT managed by either stack; remove manually if desired.

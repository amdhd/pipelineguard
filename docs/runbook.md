# PipelineGuard — Runbook

## First-time deploy

```bash
# 1. Bootstrap remote state (creates S3 bucket + DynamoDB lock, writes backend.conf)
AWS_PROFILE=your-profile ./scripts/bootstrap.sh dev ap-southeast-1

# 2. Build Lambda layers (infracost, trivy, checkov). Needs docker + curl + zip.
./scripts/deploy-gates.sh

# 3. Provide secrets out-of-band (never commit these)
export TF_VAR_slack_webhook_url="https://hooks.slack.com/services/..."
export TF_VAR_infracost_api_key="ico-..."
export TF_VAR_anthropic_api_key="sk-ant-..."

# 4. Init + apply
cd infra
terraform init -backend-config=backend.conf
terraform apply -var-file=environments/dev.tfvars

# 5. Authorise the GitHub connection ONCE in the console:
#    Developer Tools -> Settings -> Connections -> pg-dev -> Update pending connection
#    (Terraform creates it in PENDING state — this handshake cannot be automated.)
```

## Common operations

| Task | Command |
|---|---|
| Re-run the pipeline | Push to `main`, or "Release change" in the CodePipeline console |
| Change cost threshold | Edit `cost_gate_threshold` in the tfvars, `terraform apply` |
| Rebuild a gate layer | `./scripts/deploy-gates.sh` then `terraform apply` |
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

```bash
cd infra
terraform destroy -var-file=environments/dev.tfvars

# ECR images block repo deletion if any remain — force-delete first if needed:
aws ecr batch-delete-image --repository-name pipelineguard-app-dev \
  --image-ids "$(aws ecr list-images --repository-name pipelineguard-app-dev --query 'imageIds[*]' --output json)" || true

# The remote-state bucket + lock table are NOT managed by this stack; remove manually if desired.
```

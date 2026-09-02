# LAYER 2 = the demo/live stack (networking + ECR + ECS + pipeline + gates).
# Bring it up with scripts/demo-up.sh, tear it down with scripts/demo-down.sh.
# While down it bills ~$0; a demo day costs ~$2-3 (NAT + ALB are hourly and can't
# scale to zero, so "off" for this layer means destroyed).
aws_region             = "ap-southeast-1"
environment            = "dev"
owner_tag              = "amad"
github_repo            = "amdhd/pipelineguard"
github_branch          = "main"
cost_gate_threshold    = 50
log_retention_days     = 7
app_port               = 3000
ecs_cpu                = 256
ecs_memory             = 512
ecs_desired_count      = 1
enable_manual_approval = false

# No secrets belong in this file, or in any Terraform variable.
# `terraform show -json` does not redact sensitive values, so anything passed to
# Terraform is written in plaintext into plan.json — a pipeline artifact stored in
# S3. The gate API keys therefore live only in Secrets Manager, seeded with:
#
#   export INFRACOST_API_KEY=... ANTHROPIC_API_KEY=... SLACK_WEBHOOK_URL=...
#   export GITHUB_TOKEN=...        # optional; enables the security gate PR comment
#   ./scripts/seed-gate-secrets.sh dev ap-southeast-1

# QA-agent vars are deliberately NOT here — they live in layer1_persistent/dev.tfvars.
# layer1_state_* default to the shared account/region and the layer1 state key;
# override only if layer1 ever moves backend.
# layer1_state_bucket = "pipelineguard-tfstate-149751500899-ap-southeast-1"
# layer1_state_key    = "pipelineguard/layer1/dev/terraform.tfstate"
# layer1_state_region = "ap-southeast-1"

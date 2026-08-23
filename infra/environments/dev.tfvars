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

aws_region             = "ap-southeast-1"
environment            = "dev"
owner_tag              = "amad"
github_repo            = "amad/pipelineguard"
github_branch          = "main"
cost_gate_threshold    = 50
log_retention_days     = 7
app_port               = 3000
ecs_cpu                = 256
ecs_memory             = 512
ecs_desired_count      = 1
enable_manual_approval = false

# Sensitive values — do NOT commit real secrets here.
# Provide via -var, TF_VAR_* env vars, or a git-ignored *.auto.tfvars file:
#   slack_webhook_url = "https://hooks.slack.com/services/..."
#   infracost_api_key = "ico-..."
#   anthropic_api_key = "sk-ant-..."

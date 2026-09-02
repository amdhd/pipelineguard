# LAYER 1 outputs. kms_key_arn is what layer2_ephemeral consumes via
# terraform_remote_state; the qa_* outputs are the ones vesselAI workflows and
# scripts read (terraform output qa_runtime_arn, etc).

output "kms_key_arn" {
  description = "ARN of the shared CMK — read by layer2_ephemeral via terraform_remote_state"
  value       = aws_kms_key.main.arn
}

output "qa_reports_bucket" {
  description = "S3 bucket for QA agent screenshots and findings JSON"
  value       = module.qa_agent.reports_bucket_name
}

output "qa_secret_name" {
  description = "QA target credentials secret — seed with scripts/seed-qa-secret.sh"
  value       = module.qa_agent.qa_secret_name
}

output "qa_runtime_role_arn" {
  description = "Execution role ARN for the AgentCore QA runtime"
  value       = module.qa_agent.runtime_role_arn
}

output "qa_github_role_arn" {
  description = "Role ARN for the vesselAI QA workflow's aws-actions/configure-aws-credentials step"
  value       = module.qa_agent.github_role_arn
}

output "qa_runtime_arn" {
  description = "AgentCore QA runtime ARN — null until the agent zip is packaged and passed in"
  value       = module.qa_agent.runtime_arn
}

output "qa_code_bucket" {
  description = "S3 bucket for the QA agent deployment zip"
  value       = module.qa_agent.code_bucket_name
}

output "qa_fix_role_arn" {
  description = "Role the vesselAI bug-fix workflow assumes via OIDC. Null while fix_agent_enabled is false."
  value       = module.qa_agent.github_fix_role_arn
}

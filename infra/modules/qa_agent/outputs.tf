output "reports_bucket_name" {
  description = "S3 bucket holding QA screenshots and findings JSON"
  value       = aws_s3_bucket.reports.bucket
}

output "reports_bucket_arn" {
  description = "ARN of the QA reports bucket"
  value       = aws_s3_bucket.reports.arn
}

output "qa_secret_arn" {
  description = "QA target credentials secret ARN (seeded out-of-band by scripts/seed-qa-secret.sh)"
  value       = aws_secretsmanager_secret.qa_target.arn
}

output "qa_secret_name" {
  description = "QA target credentials secret name"
  value       = aws_secretsmanager_secret.qa_target.name
}

output "runtime_role_arn" {
  description = "Execution role assumed by the AgentCore runtime"
  value       = aws_iam_role.agent_runtime.arn
}

output "log_group_name" {
  description = "CloudWatch log group for the QA agent runtime"
  value       = aws_cloudwatch_log_group.agent.name
}

output "github_role_arn" {
  description = "Role the vesselAI QA workflow assumes via OIDC"
  value       = aws_iam_role.github_qa.arn
}

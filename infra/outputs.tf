output "alb_dns_name" {
  description = "Public DNS name of the application load balancer"
  value       = module.ecs.alb_dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL for the app image"
  value       = module.ecr.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = module.ecs.cluster_name
}

output "pipeline_name" {
  description = "CodePipeline name"
  value       = module.pipeline.pipeline_name
}

output "artifact_bucket" {
  description = "Pipeline artifact S3 bucket"
  value       = module.pipeline.artifact_bucket_name
}

output "github_connection_arn" {
  description = "CodeStar GitHub connection ARN — must be authorised once in the console"
  value       = module.pipeline.github_connection_arn
}

output "cost_gate_function" {
  description = "Cost gate Lambda function name"
  value       = module.gates.cost_gate_name
}

output "security_gate_function" {
  description = "Security gate Lambda function name"
  value       = module.gates.security_gate_name
}

output "security_gate_ecr_url" {
  description = "ECR repository URL for the security gate container image"
  value       = module.gates.security_gate_ecr_url
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

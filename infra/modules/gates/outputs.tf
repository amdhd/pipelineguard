output "cost_gate_arn" {
  description = "Cost gate Lambda ARN"
  value       = aws_lambda_function.cost_gate.arn
}

output "cost_gate_name" {
  description = "Cost gate Lambda function name"
  value       = aws_lambda_function.cost_gate.function_name
}

output "security_gate_arn" {
  description = "Security gate Lambda ARN"
  value       = aws_lambda_function.security_gate.arn
}

output "security_gate_name" {
  description = "Security gate Lambda function name"
  value       = aws_lambda_function.security_gate.function_name
}

output "security_gate_ecr_url" {
  description = "ECR repository URL for the security gate container image"
  value       = aws_ecr_repository.security_gate.repository_url
}

output "secrets_arn" {
  description = "Gate secrets ARN in Secrets Manager"
  value       = aws_secretsmanager_secret.gate_secrets.arn
}

output "alerts_topic_arn" {
  description = "SNS topic ARN for alerts"
  value       = aws_sns_topic.alerts.arn
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "cost_threshold" {
  description = "Max allowed monthly cost increase in USD"
  type        = number
}

variable "artifact_bucket_arn" {
  description = "Pipeline artifact bucket ARN (gates read plan/terraform artifacts)"
  type        = string
}

variable "log_retention" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 7
}

variable "security_gate_image_tag" {
  description = "Tag of the security gate container image in ECR (built by scripts/deploy-gates.sh)"
  type        = string
  default     = "latest"
}

variable "kms_key_arn" {
  description = "CMK ARN for encrypting gate secrets, logs, SNS, ECR, and Lambda env vars"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "github_repo" {
  description = "GitHub repo as owner/repo-name"
  type        = string
}

variable "github_branch" {
  description = "Branch the pipeline tracks"
  type        = string
}

variable "ecr_repo_url" {
  description = "ECR repository URL"
  type        = string
}

variable "ecr_repo_arn" {
  description = "App repository ARN for the image-build role"
  type        = string
}

variable "layer1_state_bucket" {
  description = "Persistent core state bucket; plan/deploy roles receive read access only"
  type        = string
}

variable "layer1_state_key" {
  description = "Exact persistent core state object key"
  type        = string
}

variable "ecs_cluster_name" {
  description = "ECS cluster name"
  type        = string
}

variable "ecs_service_name" {
  description = "ECS service name"
  type        = string
}

variable "cost_gate_arn" {
  description = "Cost gate Lambda ARN"
  type        = string
}

variable "cost_gate_name" {
  description = "Cost gate Lambda function name"
  type        = string
}

variable "security_gate_arn" {
  description = "Security gate Lambda ARN"
  type        = string
}

variable "security_gate_name" {
  description = "Security gate Lambda function name"
  type        = string
}

variable "enable_manual_approval" {
  description = "Insert a manual approval stage before deploy"
  type        = bool
  default     = false
}

variable "log_retention" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 7
}

variable "kms_key_arn" {
  description = "CMK ARN for encrypting artifacts, logs, CodeBuild, CodePipeline, and SSM"
  type        = string
}

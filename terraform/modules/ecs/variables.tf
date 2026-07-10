variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "private_subnets" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
}

variable "public_subnets" {
  description = "Public subnet IDs for the ALB"
  type        = list(string)
}

variable "ecr_repo_url" {
  description = "ECR repository URL"
  type        = string
}

variable "app_image_tag" {
  description = "App image tag to deploy (Git SHA in the pipeline; 'latest' for the initial manual bootstrap apply)"
  type        = string
  default     = "latest"
}

variable "app_port" {
  description = "Container port"
  type        = number
}

variable "cpu" {
  description = "Task CPU units"
  type        = number
}

variable "memory" {
  description = "Task memory (MiB)"
  type        = number
}

variable "desired_count" {
  description = "Desired task count"
  type        = number
}

variable "log_retention" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 7
}

variable "kms_key_arn" {
  description = "CMK ARN for encrypting ECS logs (and pulling the KMS-encrypted image)"
  type        = string
}

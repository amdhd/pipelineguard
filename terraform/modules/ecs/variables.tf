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

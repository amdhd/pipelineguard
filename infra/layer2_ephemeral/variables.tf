# LAYER 2 variables — the demo/live stack. No QA-agent vars here (they live in
# layer1_persistent). The layer1_state_* trio configures the terraform_remote_state
# data source that reads the shared KMS key's ARN out of layer1's state.

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-southeast-1"
}

variable "environment" {
  description = "Deployment environment (dev, prod)"
  type        = string
  default     = "dev"
}

variable "owner_tag" {
  description = "Owner tag applied to every resource"
  type        = string
  default     = "amad"
}

variable "github_repo" {
  description = "GitHub source repository as owner/repo-name"
  type        = string
}

variable "github_branch" {
  description = "Branch the pipeline tracks"
  type        = string
  default     = "main"
}

variable "cost_gate_threshold" {
  description = "Max allowed monthly cost increase in USD before the cost gate blocks"
  type        = number
  default     = 50
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days (7 dev / 30 prod)"
  type        = number
  default     = 7
}

variable "app_port" {
  description = "Port the container listens on"
  type        = number
  default     = 3000
}

variable "app_image_tag" {
  description = "App image tag to deploy (the pipeline passes the Git SHA; 'latest' for the initial manual bootstrap apply)"
  type        = string
  default     = "latest"
}

variable "ecs_cpu" {
  description = "Fargate task CPU units"
  type        = number
  default     = 256
}

variable "ecs_memory" {
  description = "Fargate task memory (MiB)"
  type        = number
  default     = 512
}

variable "ecs_desired_count" {
  description = "Desired number of running app tasks"
  type        = number
  default     = 1
}

variable "enable_manual_approval" {
  description = "Insert a manual approval stage before deploy (recommended for prod)"
  type        = bool
  default     = false
}

# --- Layer 1 remote-state pointer (reads layer1's KMS key ARN) ---
#
# Both layers share one account + region, but the backend object is separate per
# layer so teardown of one never touches the other.
variable "layer1_state_bucket" {
  description = "S3 bucket holding layer1 (persistent QA core) state"
  type        = string
  default     = "pipelineguard-tfstate-149751500899-ap-southeast-1"
}

variable "layer1_state_key" {
  description = "State object key of layer1 in the shared bucket"
  type        = string
  default     = "pipelineguard/layer1/dev/terraform.tfstate"
}

variable "layer1_state_region" {
  description = "Region the layer1 state bucket lives in"
  type        = string
  default     = "ap-southeast-1"
}

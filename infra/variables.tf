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

# --- QA agent code artifact (printed by scripts/package-qa-agent.sh) ---
#
# Empty by default so a cold apply works before any zip exists. The runtime
# resource is created only once these are supplied:
#
#   ./scripts/package-qa-agent.sh dev ap-southeast-1
#   ./scripts/apply-dev.sh -var="qa_agent_code_key=..." -var="qa_agent_code_version_id=..."
variable "qa_agent_code_key" {
  description = "S3 key of the QA agent deployment zip. Empty disables the AgentCore runtime."
  type        = string
  default     = ""
}

variable "qa_agent_code_version_id" {
  description = "S3 object version of the QA agent zip"
  type        = string
  default     = ""
}

variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "aws_region" {
  description = "AWS region (used to build the Bedrock inference-profile ARNs)"
  type        = string
}

variable "kms_key_arn" {
  description = "CMK ARN for encrypting the reports bucket, the QA secret, and logs"
  type        = string
}

variable "log_retention" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 7
}

variable "report_retention_days" {
  description = "Days before QA screenshots and findings JSON expire. Screenshots are large and worthless once the PR is merged."
  type        = number
  default     = 7
}

# Both rungs of the model benchmark (PLAN.md 1d). Current-generation Anthropic
# models on Bedrock are INFERENCE_PROFILE only -- the bare model id is not
# invocable -- so these are profile ids, not model ids. See DISCOVERY.md 11.
variable "model_profile_ids" {
  description = "Bedrock inference profile IDs the agent may invoke (cheap and quality rungs)"
  type        = list(string)
  default = [
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
  ]
}

variable "qa_workflow_repo" {
  description = "owner/repo whose GitHub Actions workflow may assume the QA role"
  type        = string
  default     = "amdhd/vesselAI"
}

variable "qa_workflow_ref" {
  description = "Git ref the schedule/workflow_dispatch triggers run on. PR runs present a different subject and are enumerated separately."
  type        = string
  default     = "refs/heads/main"
}

# LAYER 1 variables — only what the QA core needs. App/pipeline/demo vars live
# in layer2_ephemeral; the demo layer reads this layer's KMS key via
# terraform_remote_state and never duplicates the QA vars.

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

variable "log_retention_days" {
  description = "CloudWatch log retention in days (7 dev / 30 prod)"
  type        = number
  default     = 7
}

# --- QA agent code artifact (printed by scripts/package-qa-agent.sh) ---
#
# Empty by default so a cold apply works before any zip exists. The runtime
# resource is created only once these are supplied.
#
# WARNING: these are pinned in layer1_persistent/dev.tfvars ON PURPOSE (the
# 2026-08-30 runtime-destroy incident). Do not leave them out of a routine
# layer1 apply, or the runtime count drops to 0 and vesselAI's QA_RUNTIME_ARN
# goes stale.
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

# PLAN.md Phase 2. False creates no fix role at all -- the kill switch is the
# absence of the identity, not a narrower policy on one that still exists.
variable "fix_agent_enabled" {
  description = "Create the CI role the Phase 2 bug-fix harness assumes. Leave false until agents/fix/ exists; flipping it off later stops the fix agent at the account, with no workflow change."
  type        = bool
  default     = false
}

variable "qa_corpus_refs" {
  description = "Git refs allowed to assume the QA role via workflow_dispatch for seeded-corpus runs. Empty by default (strict main-only); pass e.g. [\"refs/heads/qa-corpus-1\"] to reopen a branch for a corpus dispatch, then drop the flag to restore."
  type        = list(string)
  default     = []
}

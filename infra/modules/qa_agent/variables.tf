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
  # Verified available and invocable in this account, not just listed.
  # Sonnet 5 is NOT here on purpose: it lists in the region but returns
  # AccessDeniedException ("not available for this account", contact AWS Sales).
  # Granting an ARN we cannot call would be a statement to defend in review for
  # a capability we do not have.
  default = [
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-sonnet-4-6",
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

# P3.4 rollback switch. The pull_request TRIGGER is in vesselAI's workflow and
# out of this repo's reach; the SUBJECT that trigger must assume is not.
variable "qa_pr_enabled" {
  description = "Admit the pull_request subject to the QA role's trust policy. False stops PR-triggered QA at the account -- the external workflow still fires and its assume-role fails -- with no vesselAI change. Corpus/schedule ref subjects are unaffected."
  type        = bool
  default     = true
}

variable "qa_corpus_refs" {
  description = "Extra git refs allowed to assume the QA role via workflow_dispatch, for the seeded-bug corpus (e.g. the qa-corpus-1 test branch). Empty by default so the strict main-only baseline needs no change; pass explicitly to reopen a branch for a corpus run, then drop the flag to restore."
  type        = list(string)
  default     = []
}

# --- Runtime code artifact ---
#
# Empty by default, which is what makes the cold-start ordering work: the
# runtime cannot reference a zip that does not exist yet, so it is created only
# once scripts/package-qa-agent.sh has uploaded one and printed these values.
# Same shape as the repo's existing phased apply for the gate container image.
variable "qa_agent_code_key" {
  description = "S3 key of the agent deployment zip. Empty disables the runtime resource entirely."
  type        = string
  default     = ""
}

variable "qa_agent_code_version_id" {
  description = "S3 object version of the agent zip. Pinning it makes a deploy immutable and a rollback a variable change."
  type        = string
  default     = ""
}

variable "agent_runtime_python" {
  description = "Runtime enum for the code artifact. MUST match PY_VERSION in scripts/package-qa-agent.sh -- vendored .so files are tagged cpython-3XX and will not load on another minor."
  type        = string
  default     = "PYTHON_3_12"
}

variable "idle_session_timeout" {
  description = "Seconds an idle session survives before reclamation. AWS defaults to 900; memory bills for every second a session is alive, including idle."
  type        = number
  default     = 300
}

variable "max_session_lifetime" {
  description = "Hard ceiling on a single session, in seconds."
  type        = number
  default     = 1800
}

variable "log_page_state" {
  description = "Emit the full page state (incl. empty_slots) on every read, to diagnose a run's blindness. OFF by default: it costs a log line per navigation/read, so it is an explicit opt-in for debugging, not a permanent metered tax."
  type        = bool
  default     = false
}

# --- Phase 2 bug-fix harness ---
#
# Off by default. Until the harness in agents/fix/ exists there is nothing to
# grant, and once it does, this flag is the kill switch: false means the fix
# agent cannot authenticate at all, enforced in the account rather than in
# vesselAI's workflow file. See PLAN.md Phase 2.
variable "fix_agent_enabled" {
  description = "Create the CI role the Phase 2 bug-fix harness assumes. False removes the role entirely, which is the Phase 2 kill switch."
  type        = bool
  default     = false
}

variable "fix_model_profile_ids" {
  description = "Bedrock inference profile IDs the fix harness may invoke. Deliberately separate from model_profile_ids: a cheaper rung is often fine for QA triage and is not fine for code edits."
  type        = list(string)
  # Quality rung only. Haiku is available to the QA agent as an explicit opt-in
  # for triage; nothing writes source with it.
  default = ["global.anthropic.claude-sonnet-4-6"]
}

variable "fix_workflow_refs" {
  description = "Git refs whose workflow_dispatch runs may assume the fix role. A REF list, not a subject list, so a fork-reachable pull_request subject cannot be expressed here."
  type        = list(string)
  default     = ["refs/heads/main"]
}

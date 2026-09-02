# LAYER 1 — the always-on QA core. Stays applied so the vesselAI QA workflow
# keeps working automatically; costs ~$1.40/mo idle (KMS $1 + QA secret $0.40 +
# pennies). The AgentCore runtime is PUBLIC-mode (no VPC/ENI/NAT) and bills per
# session, never while idle — see docs/agentcore/DISCOVERY.md.
#
# This is the LAYER THAT MUST NOT BE DESTROYED for routine demo teardown.
# Destroying it stops vesselAI's QA runs and (if the runtime goes) invalidates
# the QA_RUNTIME_ARN variable in amdhd/vesselAI. The demo stack is the OTHER
# layer (layer2_ephemeral); tear this one down only to stop the whole project.
#
# The CLI-only code vars live in dev.tfvars ON PURPOSE (committed): leaving them
# out of a routine apply is what destroyed the runtime once (2026-08-30). A
# routine layer1 apply MUST carry qa_agent_code_key + qa_agent_code_version_id.

module "qa_agent" {
  source                   = "../modules/qa_agent"
  environment              = var.environment
  aws_region               = var.aws_region
  log_retention            = var.log_retention_days
  kms_key_arn              = aws_kms_key.main.arn
  qa_agent_code_key        = var.qa_agent_code_key
  qa_agent_code_version_id = var.qa_agent_code_version_id
  qa_corpus_refs           = var.qa_corpus_refs
  qa_pr_enabled            = var.qa_pr_enabled
  fix_agent_enabled        = var.fix_agent_enabled
}

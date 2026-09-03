# LAYER 1 = the QA core (KMS + module.qa_agent). This root's only job is to keep
# the vesselAI QA workflow alive. There are no app/pipeline vars here — those
# belong to layer2_ephemeral, which reads this layer's KMS key via
# terraform_remote_state.
aws_region         = "ap-southeast-1"
environment        = "dev"
owner_tag          = "amad"
log_retention_days = 7

# --- QA agent deployment artifact ---
#
# COMMITTED ON PURPOSE, because leaving it out caused a real incident.
#
# The runtime resource is count-gated on qa_agent_code_key: empty means count 0.
# That gate exists so a COLD apply works on an account where the zip does not
# exist yet — apply, package, apply again. But an unset key here meant every
# ROUTINE apply also saw count 0, so `./scripts/apply-dev.sh` silently DESTROYED
# a working runtime. Worse than it sounds: AgentCore regenerates the runtime ARN
# on recreate, so the blast radius includes vesselAI's QA_RUNTIME_ARN variable,
# and the QA workflow fails afterwards for an unrelated-looking reason.
# Happened 2026-08-30; the runtime was restored from the S3 object version below.
#
# Pinning the version is what the variable's own description asks for —
# "Pinning it makes a deploy immutable and a rollback a variable change." Update
# BOTH values in the same commit that ships a new zip, so the deployed version is
# reviewable in git instead of living in someone's shell history.
#
# COLD START, after a destroy — the S3 object is gone, so pass an empty key for
# the first apply only:
#
#   ./scripts/apply-dev.sh -var qa_agent_code_key=""
#   AWS_PROFILE=pipelineguard ./scripts/package-qa-agent.sh
#   ./scripts/apply-dev.sh
qa_agent_code_key        = "agent/qa-agent-dev.zip"
qa_agent_code_version_id = "jKVLk0Z6kIv2jlGYFNr0UIztEnA_N_0V"

# --- Phase 2 bug-fix agent ---
#
# Same incident-shape as the two values above: the variable defaults to false,
# so a plain apply would have destroyed the role the fix workflow assumes — one
# day after creating it.
#
# This IS the Phase 2 kill switch (PLAN.md Phase 2). Flip to false and apply to
# revoke the fix agent's identity at the account, with no change in vesselAI.
fix_agent_enabled = true

# qa_corpus_refs stays unset here (strict main-only). Open/close a corpus branch
# via scripts/reopen-corpus.sh, which passes the var on the CLI.

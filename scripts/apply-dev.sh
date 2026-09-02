#!/usr/bin/env bash
# Convenience wrapper: applies LAYER 1 — the persistent QA core
# (infra/layer1_persistent: KMS + module.qa_agent) — with the right profile +
# var-file. This is the layer that keeps the vesselAI QA workflow alive.
#
# WARNING (hard rule): qa_agent_code_key / qa_agent_code_version_id are pinned in
# layer1_persistent/dev.tfvars ON PURPOSE. Never strip them from an apply — a
# runtime apply without them destroys the AgentCore runtime (count 1 → 0), and
# vesselAI's QA_RUNTIME_ARN goes stale. See the incident note in dev.tfvars.
#
# The demo/live stack is layer2_ephemeral; bring it up with ./scripts/demo-up.sh
# and tear it down with ./scripts/demo-down.sh.
#
# Usage: ./scripts/apply-dev.sh            (interactive, asks for 'yes')
#        ./scripts/apply-dev.sh -auto-approve   (no prompt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"

cd "${ROOT}/infra/layer1_persistent"
terraform apply -var-file=dev.tfvars "$@"

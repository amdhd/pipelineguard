#!/usr/bin/env bash
# FULL project teardown — BOTH layers, including the QA core. Run this only to
# STOP THE WHOLE PROJECT. Destroying layer1 removes the AgentCore QA runtime that
# vesselAI's QA workflow invokes: vesselAI's QA_RUNTIME_ARN goes stale until the
# runtime is recreated (cold start — see infra/layer1_persistent/dev.tfvars).
#
# For ROUTINE demo teardown use ./scripts/demo-down.sh — it destroys only the
# demo layer and leaves the QA core up.
#
# Empties the ECR repos first (Terraform can't delete non-empty repos), then
# destroys layer2 (demo) and layer1 (QA core). The Terraform state backend
# (S3 state bucket from bootstrap.sh) is intentionally left in place.
#
# Usage: ./scripts/destroy-dev.sh            (asks for confirmation)
#        ./scripts/destroy-dev.sh -auto-approve
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"
REGION="ap-southeast-1"

echo "==> FULL teardown: this destroys the QA core too (vesselAI QA goes dark)."
echo "    Use ./scripts/demo-down.sh instead for routine demo teardown."
read -r -p "Continue with FULL teardown of both layers? [y/N] " answer
if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 1
fi

# Empty ECR repos so 'terraform destroy' can remove them.
for repo in pipelineguard-app-dev pipelineguard-security-gate-dev; do
  ids="$(aws ecr list-images --repository-name "$repo" --region "$REGION" \
    --query 'imageIds[*]' --output json 2>/dev/null || echo '[]')"
  if [ "$ids" != "[]" ] && [ -n "$ids" ]; then
    echo "==> Emptying ECR repo $repo..."
    aws ecr batch-delete-image --repository-name "$repo" --region "$REGION" \
      --image-ids "$ids" >/dev/null 2>&1 || true
  fi
done

echo "==> Destroying layer2 (demo stack)..."
cd "${ROOT}/infra/layer2_ephemeral"
terraform destroy -var-file=dev.tfvars "$@"

echo "==> Destroying layer1 (QA core)..."
cd "${ROOT}/infra/layer1_persistent"
terraform destroy -var-file=dev.tfvars "$@"

echo ""
echo "==> Full teardown complete. Verify no NAT Gateway remains (the main cost):"
echo "    aws ec2 describe-nat-gateways --region ${REGION} \\"
echo "      --filter Name=tag:Project,Values=pipelineguard Name=state,Values=available"

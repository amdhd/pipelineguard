#!/usr/bin/env bash
# Tears down ONLY the demo/live layer (layer2_ephemeral: networking + ECR + ECS +
# pipeline + gates) and leaves the always-on QA core (layer1_persistent: KMS +
# module.qa_agent) untouched. NAT + ALB are the ~$50/mo idle cost — this is how
# they get turned off between demos.
#
# Layer2 has its OWN Terraform state, so this destroy can never touch layer1 by
# construction — the runtime-destroy hazard that routine teardown used to carry
# (CLAUDE.md hard rule 1) is gone.
#
# Usage: ./scripts/demo-down.sh            (asks for confirmation)
#        ./scripts/demo-down.sh -auto-approve
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"
REGION="ap-southeast-1"

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

cd "${ROOT}/infra/layer2_ephemeral"
terraform destroy -var-file=dev.tfvars "$@"

echo ""
echo "==> Demo layer destroyed. QA core (layer1: KMS + qa_agent) is untouched."
echo "    Verify no NAT Gateway remains (the main cost):"
echo "    aws ec2 describe-nat-gateways --region ${REGION} \\"
echo "      --filter Name=tag:Project,Values=pipelineguard Name=state,Values=available"
echo "    Layer1 still up: cd infra/layer1_persistent && terraform state list"

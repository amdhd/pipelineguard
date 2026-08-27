#!/usr/bin/env bash
# Tears down the dev stack. Empties the ECR repos first (Terraform can't delete
# non-empty repos), then destroys everything. The Terraform state backend
# (S3 state bucket from bootstrap.sh) is intentionally left in place.
#
# Usage: ./scripts/destroy-dev.sh            (asks for confirmation)
#        ./scripts/destroy-dev.sh -auto-approve
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

cd "${ROOT}/infra"
terraform destroy -var-file=environments/dev.tfvars "$@"

echo ""
echo "==> Destroyed. Verify no NAT Gateway remains (the main cost):"
echo "    aws ec2 describe-nat-gateways --region ${REGION} \\"
echo "      --filter Name=tag:Project,Values=pipelineguard Name=state,Values=available"

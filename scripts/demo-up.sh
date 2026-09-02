#!/usr/bin/env bash
# Brings up the demo/live layer (layer2_ephemeral: networking + ECR + ECS +
# pipeline + gates) for a demo session. While this layer is down it bills ~$0;
# a demo day costs ~$2-3 (NAT + ALB are hourly and can't scale to zero).
#
# Does the cold-start two-phase apply (mirroring docs/runbook.md): the security
# gate Lambda image must exist in ECR BEFORE the apply that creates the Lambda,
# so phase 1 creates just that repo, pushes the image, then phase 2 applies the
# rest. The app image is pushed after phase 2 so the ECS service can actually
# start (the task definition references :latest).
#
# Layer1 (the QA core) is NOT touched. Tear the demo down afterwards with
# ./scripts/demo-down.sh.
#
# Usage: ./scripts/demo-up.sh             (interactive)
#        ./scripts/demo-up.sh -auto-approve
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"
REGION="ap-southeast-1"

cd "${ROOT}/infra/layer2_ephemeral"

echo "==> [1/5] Creating security-gate ECR repo (needed before its image push)..."
terraform apply -var-file=dev.tfvars "$@" \
  -target=module.gates.aws_ecr_repository.security_gate

echo "==> [2/5] Building + pushing security-gate image..."
"${ROOT}/scripts/deploy-gates.sh"

echo "==> [3/5] Applying the rest of the demo layer (VPC/NAT/ECS/ALB/pipeline/gates)..."
terraform apply -var-file=dev.tfvars "$@"

echo "==> [4/5] Pushing the app image (:latest) so the ECS service can start..."
ECR_URL="$(terraform output -raw ecr_repository_url)"
REGISTRY="${ECR_URL%%/*}"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null
# Build for linux/amd64 to match the Fargate default platform (critical on
# Apple Silicon, where a plain `docker build` would produce an arm64 image).
docker buildx inspect pg-builder >/dev/null 2>&1 \
  || docker buildx create --name pg-builder --driver docker-container >/dev/null
docker buildx build --builder pg-builder --platform linux/amd64 \
  -t "${ECR_URL}:latest" \
  --push \
  "${ROOT}/app"
echo "    -> ${ECR_URL}:latest"

echo ""
echo "==> [5/5] Demo layer is up."
echo ""
echo "    If this is the first demo on this account, AUTHORISE the CodeStar"
echo "    connection once in the console:"
echo "      Settings → Connections → (github connection) → Update pending connection"
echo ""
echo "    App URL:      http://$(terraform output -raw alb_dns_name)"
echo "    Pipeline:     $(terraform output -raw pipeline_name)"
echo ""
echo "    Tear it down afterwards with ./scripts/demo-down.sh"

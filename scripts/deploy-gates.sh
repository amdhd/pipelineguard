#!/usr/bin/env bash
#
# Packages the gate build artifacts:
#   1. Cost gate    -> infracost binary Lambda layer (small; fits a zip Lambda).
#   2. Security gate -> container image (Trivy + Checkov exceed Lambda's 250 MB
#      unzipped zip limit, so it runs as an image). See gates/security_gate/Dockerfile.
#
# The security gate ECR repo must already exist before this pushes the image, so
# the first-time flow is two-phase (see docs/runbook.md):
#   terraform apply -target=module.gates.aws_ecr_repository.security_gate
#   ./scripts/deploy-gates.sh
#   terraform apply -var-file=environments/dev.tfvars
#
# Usage: AWS_PROFILE=... ./scripts/deploy-gates.sh [environment] [region]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYER_DIR="${ROOT}/gates/layers"
ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"
IMAGE_TAG="${SECURITY_GATE_IMAGE_TAG:-latest}"
mkdir -p "${LAYER_DIR}"

INFRACOST_VERSION="${INFRACOST_VERSION:-v0.10.39}"

# --- Cost gate: infracost binary layer ---
echo "==> Building infracost layer..."
work="$(mktemp -d)"
mkdir -p "${work}/layer"
curl -fsSL "https://github.com/infracost/infracost/releases/download/${INFRACOST_VERSION}/infracost-linux-amd64.tar.gz" \
  | tar -xz -C "${work}"
mv "${work}/infracost-linux-amd64" "${work}/layer/infracost"
chmod +x "${work}/layer/infracost"
( cd "${work}/layer" && zip -qr "${LAYER_DIR}/infracost_layer.zip" . )
rm -rf "${work}"
echo "    -> ${LAYER_DIR}/infracost_layer.zip"

# --- Security gate: container image (Trivy + Checkov) ---
echo "==> Resolving security gate ECR repository..."
ECR_URL="$(aws ecr describe-repositories \
  --repository-names "pipelineguard-security-gate-${ENVIRONMENT}" \
  --region "${REGION}" \
  --query 'repositories[0].repositoryUri' --output text 2>/dev/null || true)"

if [ -z "${ECR_URL}" ] || [ "${ECR_URL}" = "None" ]; then
  echo "ERROR: ECR repo pipelineguard-security-gate-${ENVIRONMENT} not found." >&2
  echo "       Create it first, then re-run this script:" >&2
  echo "       cd terraform && terraform apply -target=module.gates.aws_ecr_repository.security_gate" >&2
  exit 1
fi
REGISTRY="${ECR_URL%%/*}"
echo "    repo: ${ECR_URL}"

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# Build for linux/amd64 to match the Lambda function architecture (important on
# Apple Silicon, where docker would otherwise produce an arm64 image).
#
# --provenance=false is REQUIRED: without it, buildx emits an OCI image index
# with an attestation manifest, and Lambda rejects it with "The image manifest,
# config or layer media type ... is not supported." Disabling provenance yields
# a plain Docker v2 schema-2 manifest that Lambda accepts.
echo "==> Building + pushing security gate image (linux/amd64)..."
# The docker-container driver reliably supports --push and --provenance=false
# (the default 'docker' driver may not, depending on Docker Desktop settings).
docker buildx inspect pg-builder >/dev/null 2>&1 \
  || docker buildx create --name pg-builder --driver docker-container >/dev/null
docker buildx build --builder pg-builder --platform linux/amd64 \
  --provenance=false \
  -t "${ECR_URL}:${IMAGE_TAG}" \
  --push \
  "${ROOT}/gates/security_gate"
echo "    -> ${ECR_URL}:${IMAGE_TAG}"

echo "==> Done. Now run: cd terraform && terraform apply -var-file=environments/dev.tfvars"

#!/usr/bin/env bash
#
# Packages the Lambda layers (infracost, trivy, checkov + python deps) that the
# gate functions depend on, then lets Terraform pick them up on the next apply.
#
# Layers are built with Docker to match the Lambda python3.12 runtime.
#
# Usage: ./scripts/deploy-gates.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYER_DIR="${ROOT}/gates/layers"
mkdir -p "${LAYER_DIR}"

INFRACOST_VERSION="${INFRACOST_VERSION:-v0.10.39}"
TRIVY_VERSION="${TRIVY_VERSION:-0.53.0}"

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

echo "==> Building trivy layer..."
work="$(mktemp -d)"
mkdir -p "${work}/layer"
curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
  | tar -xz -C "${work}"
mv "${work}/trivy" "${work}/layer/trivy"
chmod +x "${work}/layer/trivy"
( cd "${work}/layer" && zip -qr "${LAYER_DIR}/trivy_layer.zip" . )
rm -rf "${work}"
echo "    -> ${LAYER_DIR}/trivy_layer.zip"

echo "==> Building checkov + python deps layer (Docker, python3.12)..."
work="$(mktemp -d)"
docker run --rm --entrypoint /bin/bash \
  -v "${work}:/out" \
  public.ecr.aws/lambda/python:3.12 -c "
    pip install --no-cache-dir -t /out/python \
      checkov anthropic requests >/dev/null &&
    find /out/python -name '__pycache__' -type d -prune -exec rm -rf {} +
  "
( cd "${work}" && zip -qr "${LAYER_DIR}/checkov_layer.zip" python )
rm -rf "${work}"
echo "    -> ${LAYER_DIR}/checkov_layer.zip"

echo "==> Layers built. Run: cd terraform && terraform apply -var-file=environments/dev.tfvars"

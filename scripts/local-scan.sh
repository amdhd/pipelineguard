#!/usr/bin/env bash
#
# Run the security gate's scanners locally — no AWS needed. Handy for validating
# the app image + Terraform before pushing.
#
# Requires: docker, trivy, checkov on PATH.
#
# Usage: ./scripts/local-scan.sh [image-tag]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="${1:-pipelineguard-app:local}"

echo "==> Building app image ${IMAGE_TAG}..."
docker build -t "${IMAGE_TAG}" "${ROOT}/app"

echo
echo "==> Trivy: container CVE scan (HIGH/CRITICAL only)"
if command -v trivy >/dev/null 2>&1; then
  trivy image --severity HIGH,CRITICAL --exit-code 0 "${IMAGE_TAG}"
else
  echo "    trivy not installed — see https://aquasecurity.github.io/trivy/"
fi

echo
echo "==> Checkov: Terraform static analysis"
if command -v checkov >/dev/null 2>&1; then
  checkov --directory "${ROOT}/infra" --compact --quiet || true
else
  echo "    checkov not installed — 'pip install checkov'"
fi

echo
echo "==> Local scan complete. This mirrors what the security gate Lambda runs in CI."

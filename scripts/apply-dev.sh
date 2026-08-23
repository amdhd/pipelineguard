#!/usr/bin/env bash
# Convenience wrapper: applies the dev stack with the right profile + var-file.
# Usage: ./scripts/apply-dev.sh            (interactive, asks for 'yes')
#        ./scripts/apply-dev.sh -auto-approve   (no prompt)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"

cd "${ROOT}/infra"
terraform apply -var-file=environments/dev.tfvars "$@"

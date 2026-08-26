#!/usr/bin/env bash
#
# One-time account bootstrap: creates the S3 bucket that backs the Terraform
# remote state, then writes a backend.conf for `terraform init`.
#
# No lock table. The backend uses S3-native locking (`use_lockfile = true` in
# infra/versions.tf), which keeps the lock as a "<key>.tflock" object beside the
# state -- so state and lock live in one bucket with one lifecycle, and there is
# no second service to bootstrap, pay for, or forget to tear down.
#
# Usage: AWS_PROFILE=... ./scripts/bootstrap.sh [environment] [region]
set -euo pipefail

ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

STATE_BUCKET="pipelineguard-tfstate-${ACCOUNT_ID}-${REGION}"

echo "==> Account:     ${ACCOUNT_ID}"
echo "==> Region:      ${REGION}"
echo "==> State bucket: ${STATE_BUCKET}"

# --- State bucket (versioned + encrypted + private) ---
if ! aws s3api head-bucket --bucket "${STATE_BUCKET}" 2>/dev/null; then
  echo "==> Creating state bucket..."
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
  aws s3api put-bucket-versioning --bucket "${STATE_BUCKET}" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "${STATE_BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "${STATE_BUCKET}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
else
  echo "==> State bucket already exists."
fi

# --- backend.conf for terraform init ---
BACKEND_CONF="infra/backend.conf"
cat > "${BACKEND_CONF}" <<EOF
bucket         = "${STATE_BUCKET}"
key            = "pipelineguard/${ENVIRONMENT}/terraform.tfstate"
region         = "${REGION}"
encrypt        = true
EOF

echo "==> Wrote ${BACKEND_CONF}"
echo "==> Bootstrap complete. Next:"
echo "    cd infra && terraform init -backend-config=backend.conf"

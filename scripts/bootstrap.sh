#!/usr/bin/env bash
#
# One-time account bootstrap: creates the S3 bucket + DynamoDB lock table that
# back the Terraform remote state, then writes a backend.conf for `terraform init`.
#
# Usage: AWS_PROFILE=... ./scripts/bootstrap.sh [environment] [region]
set -euo pipefail

ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

STATE_BUCKET="pipelineguard-tfstate-${ACCOUNT_ID}-${REGION}"
LOCK_TABLE="pipelineguard-tflock-${ENVIRONMENT}"

echo "==> Account:     ${ACCOUNT_ID}"
echo "==> Region:      ${REGION}"
echo "==> State bucket: ${STATE_BUCKET}"
echo "==> Lock table:   ${LOCK_TABLE}"

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

# --- Lock table ---
if ! aws dynamodb describe-table --table-name "${LOCK_TABLE}" --region "${REGION}" >/dev/null 2>&1; then
  echo "==> Creating DynamoDB lock table..."
  aws dynamodb create-table \
    --table-name "${LOCK_TABLE}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${REGION}" >/dev/null
  aws dynamodb wait table-exists --table-name "${LOCK_TABLE}" --region "${REGION}"
else
  echo "==> Lock table already exists."
fi

# --- backend.conf for terraform init ---
BACKEND_CONF="terraform/backend.conf"
cat > "${BACKEND_CONF}" <<EOF
bucket         = "${STATE_BUCKET}"
key            = "pipelineguard/${ENVIRONMENT}/terraform.tfstate"
region         = "${REGION}"
dynamodb_table = "${LOCK_TABLE}"
encrypt        = true
EOF

echo "==> Wrote ${BACKEND_CONF}"
echo "==> Bootstrap complete. Next:"
echo "    cd terraform && terraform init -backend-config=backend.conf"

#!/usr/bin/env bash
#
# One-time account bootstrap: creates the account-level singletons every stack
# here depends on -- the S3 bucket backing Terraform remote state and the GitHub
# Actions OIDC provider -- then writes a backend.conf for `terraform init`.
#
# Everything created here deliberately OUTLIVES `terraform destroy`; see
# scripts/destroy-dev.sh, which leaves it alone on purpose.
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

# --- GitHub Actions OIDC provider (account-level singleton) ---
#
# WHY THIS LIVES IN BOOTSTRAP AND NOT IN A TERRAFORM STACK
#
# IAM permits exactly ONE OIDC provider per issuer URL per account, but two
# stacks in this account want one: PipelineGuard (so vesselAI's QA workflow can
# assume a role here) and vesselAI's own terraform/github-oidc.tf. Whichever
# applies second fails with EntityAlreadyExists, and whichever destroys first
# silently breaks the other's role trust.
#
# An account-singleton that outlives every stack is exactly what bootstrap is
# for -- the same argument as the state bucket, which destroy-dev.sh also leaves
# in place on purpose. Terraform stacks reference it with a data lookup and
# never manage it.
#
# KNOWN FOLLOW-UP: vesselAI's terraform/github-oidc.tf still CREATES the
# provider. Applying that stack while this one exists will fail. The fix is to
# convert it to a data lookup too; it is not done here because that repo's
# state is local and its cluster is torn down.
OIDC_URL="https://token.actions.githubusercontent.com"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_ARN}" >/dev/null 2>&1; then
  echo "==> GitHub OIDC provider already exists."
else
  echo "==> Creating GitHub OIDC provider..."
  # Compute the thumbprint from the live chain rather than hardcoding one.
  # Amazon's root fingerprint has rotated before, and a stale thumbprint breaks
  # every assume-role through this provider at once.
  THUMBPRINT="$(openssl s_client -servername token.actions.githubusercontent.com \
    -showcerts -connect token.actions.githubusercontent.com:443 </dev/null 2>/dev/null \
    | awk '/BEGIN CERT/,/END CERT/' \
    | openssl x509 -fingerprint -sha1 -noout \
    | sed 's/.*=//; s/://g' | tr 'A-Z' 'a-z')"
  if [ -z "${THUMBPRINT}" ]; then
    echo "ERROR: could not compute the OIDC thumbprint." >&2
    exit 1
  fi
  aws iam create-open-id-connect-provider \
    --url "${OIDC_URL}" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "${THUMBPRINT}" >/dev/null
  echo "==> Created ${OIDC_ARN}"
fi

# --- backend.conf for each layer's terraform init ---
# Two separate roots (layer1_persistent = QA core, always up; layer2_ephemeral =
# demo stack), each with its OWN state object so demo teardown can never touch
# the QA core. backend.conf is gitignored; the *.example files are committed.
cat > "infra/layer1_persistent/backend.conf" <<EOF
bucket         = "${STATE_BUCKET}"
key            = "pipelineguard/layer1/${ENVIRONMENT}/terraform.tfstate"
region         = "${REGION}"
encrypt        = true
EOF

cat > "infra/layer2_ephemeral/backend.conf" <<EOF
bucket         = "${STATE_BUCKET}"
key            = "pipelineguard/layer2/${ENVIRONMENT}/terraform.tfstate"
region         = "${REGION}"
encrypt        = true
EOF

echo "==> Wrote infra/layer1_persistent/backend.conf"
echo "==> Wrote infra/layer2_ephemeral/backend.conf"
echo "==> Bootstrap complete. Next, per layer:"
echo "    cd infra/layer1_persistent && terraform init -backend-config=backend.conf"
echo "    cd infra/layer2_ephemeral && terraform init -backend-config=backend.conf"

#!/usr/bin/env bash
#
# Seeds the gate API keys into Secrets Manager.
#
# These keys are deliberately NOT Terraform variables. `terraform show -json`
# does not redact sensitive values, so anything passed to Terraform is written in
# plaintext into plan.json — which ships as a pipeline artifact to S3. Keeping the
# material out of Terraform is what keeps it out of the plan.
#
# Terraform creates the (empty) secret container; this script fills it. Run it once
# after the first apply, and again whenever a key rotates.
#
# Usage:
#   export INFRACOST_API_KEY=... ANTHROPIC_API_KEY=... SLACK_WEBHOOK_URL=...
#   export GITHUB_TOKEN=...            # optional; enables the security gate PR comment
#   AWS_PROFILE=... ./scripts/seed-gate-secrets.sh [environment] [region]
#
# Unset variables keep whatever the secret already holds, so a single key can be
# rotated without re-supplying the others.
set -euo pipefail

ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"
SECRET_ID="pipelineguard/gates/${ENVIRONMENT}"

# Read the current value so unset variables are preserved. A secret with no
# version yet (first run) is not an error — start from an empty object.
EXISTING="$(aws secretsmanager get-secret-value \
  --secret-id "${SECRET_ID}" --region "${REGION}" \
  --query SecretString --output text 2>/dev/null || true)"
if [ -z "${EXISTING}" ] || [ "${EXISTING}" = "None" ]; then
  EXISTING='{}'
fi

# Write the merged payload to a 0600 temp file rather than passing it on the
# command line, where it would be visible in `ps` to any user on the host.
umask 077
PAYLOAD_FILE="$(mktemp)"
trap 'rm -f "${PAYLOAD_FILE}"' EXIT

EXISTING="${EXISTING}" PAYLOAD_FILE="${PAYLOAD_FILE}" python3 <<'PY'
import json
import os

REQUIRED = ("INFRACOST_API_KEY", "ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL")
OPTIONAL = ("GITHUB_TOKEN",)

try:
    current = json.loads(os.environ["EXISTING"])
    if not isinstance(current, dict):
        current = {}
except ValueError:
    current = {}

for key in REQUIRED + OPTIONAL:
    value = os.environ.get(key)
    if value:
        current[key] = value
current.setdefault("GITHUB_TOKEN", "")

missing = [k for k in REQUIRED if not current.get(k)]
if missing:
    raise SystemExit(
        "ERROR: no value supplied and none stored for: " + ", ".join(missing) + "\n"
        "       export them and re-run, e.g. export ANTHROPIC_API_KEY=..."
    )

with open(os.environ["PAYLOAD_FILE"], "w", encoding="utf-8") as fh:
    json.dump(current, fh)

# Report which keys are set, never their values.
print("    keys stored: " + ", ".join(
    f"{k}={'set' if current.get(k) else 'empty'}" for k in REQUIRED + OPTIONAL
))
PY

aws secretsmanager put-secret-value \
  --secret-id "${SECRET_ID}" \
  --region "${REGION}" \
  --secret-string "file://${PAYLOAD_FILE}" >/dev/null

echo "==> Seeded ${SECRET_ID} in ${REGION}"
echo "==> The gate Lambdas read this at runtime; nothing sensitive enters Terraform."

#!/usr/bin/env bash
#
# Seeds the QA agent's target credentials into Secrets Manager.
#
# Same reasoning as scripts/seed-gate-secrets.sh: these are deliberately NOT
# Terraform variables. `terraform show -json` does not redact sensitive values,
# so anything passed to Terraform is written in plaintext into plan.json — which
# ships as a pipeline artifact to S3. Keeping the material out of Terraform is
# what keeps it out of the plan.
#
# Terraform creates the (empty) secret container; this script fills it. Run it
# once after the apply that creates the secret, and again whenever a value
# changes.
#
# Usage:
#   export QA_TARGET_URL=https://...            # optional; the workflow passes this per-run
#   export QA_TARGET_EMAIL=demo@petronas.com
#   export QA_TARGET_PASSWORD=...
#   AWS_PROFILE=pipelineguard ./scripts/seed-qa-secret.sh [environment] [region]
#
# Unset variables keep whatever the secret already holds, so one value can be
# rotated without re-supplying the others.
#
# HONESTY NOTE: the current QA target is vesselAI, whose demo credentials are
# published in its public README. This protects nothing today. It is still the
# right pattern — it costs nothing, it keeps credentials out of workflow YAML,
# and it is already correct if the target ever becomes an app with real data.
# Do not describe it as protecting a secret while the target is vesselAI.
set -euo pipefail

ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"
SECRET_ID="pipelineguard/qa-agent/${ENVIRONMENT}"

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

REQUIRED = ("QA_TARGET_EMAIL", "QA_TARGET_PASSWORD")
OPTIONAL = ("QA_TARGET_URL",)

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
current.setdefault("QA_TARGET_URL", "")

missing = [k for k in REQUIRED if not current.get(k)]
if missing:
    raise SystemExit(
        "ERROR: no value supplied and none stored for: " + ", ".join(missing) + "\n"
        "       export them and re-run, e.g. export QA_TARGET_EMAIL=..."
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
echo "==> The AgentCore runtime reads this at invoke time; nothing sensitive enters Terraform."

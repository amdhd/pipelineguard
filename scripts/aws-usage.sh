#!/usr/bin/env bash
# aws-usage.sh — one-shot "what's running & what's it costing" snapshot.
#
# For a brand-new account where the billing console is still empty, this is the
# fastest way to answer two questions:
#   1. What billable resources are UP right now?           (instant, no lag)
#   2. Roughly what has it cost month-to-date?             (Cost Explorer, ~24h lag)
#
# CloudWatch shows usage metrics (CPU, invocations), not dollars — so for cost
# we hit Cost Explorer's get-cost-and-usage API instead.
#
# Usage: ./scripts/aws-usage.sh
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-pipelineguard}"
export AWS_REGION="${AWS_REGION:-ap-southeast-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

bold "== AWS usage snapshot =="
dim  "profile=${AWS_PROFILE}  region=${AWS_REGION}  account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '??')"
echo

# --- Resolve the ECS cluster name from Terraform state (falls back gracefully) ---
# The cluster lives in layer2_ephemeral (the demo layer); when it is down the
# output is empty and this reports $0 Fargate.
CLUSTER="$(terraform -chdir="${ROOT}/infra/layer2_ephemeral" output -raw ecs_cluster_name 2>/dev/null || true)"

# ---------------------------------------------------------------------------
bold "1. ECS / Fargate (main cost driver)"
if [[ -n "${CLUSTER}" ]]; then
  SERVICES="$(aws ecs list-services --cluster "${CLUSTER}" --query 'serviceArns' --output text 2>/dev/null || true)"
  if [[ -n "${SERVICES}" ]]; then
    aws ecs describe-services --cluster "${CLUSTER}" --services ${SERVICES} \
      --query 'services[].{Service:serviceName,Desired:desiredCount,Running:runningCount,Pending:pendingCount}' \
      --output table
  else
    echo "  cluster ${CLUSTER}: no services (nothing running — \$0 Fargate)"
  fi
else
  echo "  (no ecs_cluster_name output — stack may be destroyed → \$0 Fargate)"
fi
echo

# ---------------------------------------------------------------------------
bold "2. Other always-on billable resources"
echo "  EC2 instances (running):"
aws ec2 describe-instances --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType]' --output text 2>/dev/null \
  | sed 's/^/    /' || true
echo "  NAT gateways (available — ~\$0.045/hr each):"
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/    /' || true
echo "  Load balancers (~\$0.02/hr each):"
aws elbv2 describe-load-balancers \
  --query 'LoadBalancers[].LoadBalancerName' --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/    /' || true
echo "  RDS instances:"
aws rds describe-db-instances \
  --query 'DBInstances[].DBInstanceIdentifier' --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/    /' || true
echo

# ---------------------------------------------------------------------------
bold "3. Month-to-date cost by service (Cost Explorer, ~24h lag)"
START="$(date -u +%Y-%m-01)"
END="$(date -u -v+1d +%Y-%m-%d 2>/dev/null || date -u -d '+1 day' +%Y-%m-%d)"
if aws ce get-cost-and-usage \
    --time-period Start="${START}",End="${END}" \
    --granularity MONTHLY \
    --metrics UnblendedCost \
    --group-by Type=DIMENSION,Key=SERVICE \
    --query 'ResultsByTime[0].Groups[?Metrics.UnblendedCost.Amount!=`0`].[Keys[0],Metrics.UnblendedCost.Amount]' \
    --output table 2>/tmp/ce_err; then
  TOTAL="$(aws ce get-cost-and-usage \
    --time-period Start="${START}",End="${END}" \
    --granularity MONTHLY --metrics UnblendedCost \
    --query 'ResultsByTime[0].Total.UnblendedCost.Amount' --output text 2>/dev/null || echo '?')"
  bold "   MTD TOTAL: \$${TOTAL}  (${START} → today)"
else
  echo "  Cost Explorer not available yet:"
  sed 's/^/    /' /tmp/ce_err
  echo "  → Enable once in console: Billing → Cost Explorer (takes ~24h to populate)."
fi

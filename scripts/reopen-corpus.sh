#!/usr/bin/env bash
# One-shot: reopen the QA role trust policy to workflow_dispatch on qa-corpus-1
# for a seeded-corpus run. Idempotent; drop the flag / re-apply without it to
# restore the strict main-only baseline.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../infra/layer1_persistent"
AWS_PROFILE="${AWS_PROFILE:-pipelineguard}" terraform apply \
  -var-file=dev.tfvars \
  -auto-approve \
  -var='qa_corpus_refs=["refs/heads/qa-corpus-1"]' \
  -target='module.qa_agent.aws_iam_role.github_qa'

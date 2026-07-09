# PipelineGuard — CLAUDE.md

## Project Overview

**PipelineGuard** is a production-grade AWS-native CI/CD pipeline for a sample Node.js API,
with two custom quality gates built on Lambda:

1. **Cost Gate** — runs `infracost` on every Terraform plan, blocks the deploy if projected
   monthly cost delta exceeds a configurable threshold (default: $50/month increase)
2. **Security Gate** — runs Trivy (container CVE scan) + Checkov (IaC static analysis),
   calls Claude API to summarise findings, blocks deploy on any HIGH/CRITICAL severity issue,
   posts a human-readable report as a GitHub PR comment

**Goal:** A portfolio project that demonstrates DevOps + DevSecOps + FinOps skills in one repo.
Every AWS resource is provisioned via Terraform. Nothing is click-ops.

See `docs/architecture.md` and `docs/runbook.md` for details. This file is the design spec;
the repository is the implementation.

## Key Non-Negotiables

1. **Everything is Terraform.** No manual click-ops anywhere.
2. **Least-privilege IAM everywhere.** Each Lambda, CodeBuild project, and ECS task gets its own role.
3. **Secrets in Secrets Manager.** The `get_secrets()` pattern in the gate handlers is mandatory.
4. **Default tags on all resources.** `Project`, `Environment`, `ManagedBy`, `Owner`.
5. **Gate failures must never silently pass.** Always `put_job_failure_result` on error.
6. **ECS deployment circuit breaker** with rollback enabled.
7. **ECR image tags are immutable.** Git commit SHA as the image tag.
8. **CloudWatch log retention.** 7 days dev, 30 days prod.
9. **S3 versioning on artifact bucket.**
10. **Python typing.** Type hints + docstrings on all handlers.

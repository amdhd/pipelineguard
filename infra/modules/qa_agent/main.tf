data "aws_caller_identity" "current" {}

locals {
  name_prefix = "pipelineguard-qa-${var.environment}"

  # Invoking through an inference profile needs the action on THREE ARNs per
  # model, not one: the profile itself, plus each foundation model the profile
  # routes to (one region-agnostic, one region-scoped). Granting only the
  # profile ARN yields AccessDenied at invoke time with a message that points at
  # the model, not the profile -- an unpleasant thing to debug.
  #
  # The foundation-model ARNs carry an EMPTY account field because the models
  # are AWS-owned, not account-owned. Hence ":::" and ":${var.aws_region}::".
  # Verified against get-inference-profile; see DISCOVERY.md 11.
  model_arns = flatten([
    for id in var.model_profile_ids : [
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${id}",
      "arn:aws:bedrock:::foundation-model/${replace(id, "global.", "")}",
      "arn:aws:bedrock:${var.aws_region}::foundation-model/${replace(id, "global.", "")}",
    ]
  ])

  # The agent writes only under these two prefixes. Scoping the role to them
  # rather than the whole bucket is what lets the bucket hold anything else
  # later without silently widening the agent's reach.
  report_prefixes = ["screenshots/*", "reports/*"]
}

# --- Reports bucket: screenshots + findings JSON ---
#
# The agent writes here directly (its execution role has PutObject), so the QA
# workflow needs no S3 permission at all -- that is what keeps the CI-reachable
# OIDC role at "invoke one runtime, read one secret". See PLAN.md 1a.
resource "aws_s3_bucket" "reports" {
  # checkov:skip=CKV_AWS_18:Access logging omitted for a cost-conscious demo (would need a second log bucket) -- matches the pipeline artifact bucket.
  # checkov:skip=CKV_AWS_144:Cross-region replication is unnecessary for artefacts that expire in 7 days.
  # checkov:skip=CKV2_AWS_62:No event notifications -- nothing consumes bucket events.
  bucket        = "${local.name_prefix}-reports-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # portfolio: allow terraform destroy to clean up
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms" # CKV_AWS_145
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Screenshots are large and worthless once the PR is merged (PLAN.md 1d).
# The expiry also bounds presigned screenshot URLs: a link handed to a reviewer
# cannot outlive the object it points at.
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire-qa-artifacts"
    status = "Enabled"
    filter {}
    expiration {
      days = var.report_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1 # CKV_AWS_300
    }
  }
}

# This bucket holds screenshots of a running application. The repo is public;
# the bucket must not be.
resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- QA target credentials: Terraform owns the container, never the material ---
#
# Same reasoning as the gate secret in modules/gates: `terraform show -json`
# does not redact sensitive values, so anything reaching a Terraform variable is
# written in plaintext into plan.json, which ships to S3 as a pipeline artifact.
# Seeded out-of-band by scripts/seed-qa-secret.sh.
#
# Worth being honest about in an interview: the credentials this protects
# (demo@petronas.com) are already published in vesselAI's public README. This is
# the right *pattern* and it costs nothing to follow, but it is not guarding a
# secret today. It will be if the QA target ever changes.
resource "aws_secretsmanager_secret" "qa_target" {
  # checkov:skip=CKV2_AWS_57:Auto-rotation N/A -- these are an application's demo login, rotated by the target app's owner, not by AWS.
  name       = "pipelineguard/qa-agent/${var.environment}"
  kms_key_id = var.kms_key_arn
}

# --- AgentCore runtime execution role ---
resource "aws_iam_role" "agent_runtime" {
  name = "${local.name_prefix}-runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard: without these, any AgentCore runtime in any
      # account could be pointed at this role by its ARN.
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "agent_runtime" {
  # checkov:skip=CKV_AWS_355:"*" appears only on PutMetricData and log-group creation, neither of which supports resource scoping. PutMetricData is namespace-conditioned instead.
  name = "${local.name_prefix}-runtime-policy"
  role = aws_iam_role.agent_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Both benchmark rungs, every ARN enumerated. Notably NOT "*" -- see the
        # model_arns local above for why this is three ARNs per model.
        Sid      = "InvokeBenchmarkModels"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = local.model_arns
      },
      {
        # Write screenshots and findings. GetObject is here so the agent can
        # presign the screenshot URLs it returns -- a presigner must be able to
        # perform the operation it signs for.
        Sid    = "WriteReports"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = [
          for p in local.report_prefixes : "${aws_s3_bucket.reports.arn}/${p}"
        ]
      },
      {
        # SSE-KMS: Decrypt is required to presign a GET of an encrypted object,
        # GenerateDataKey to write one.
        Sid      = "UseCmk"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [var.kms_key_arn]
      },
      {
        Sid      = "ReadQaCredentials"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.qa_target.arn]
      },
      {
        Sid      = "WriteLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.agent.arn}:*"]
      },
      {
        # CreateLogGroup cannot be scoped to a group that does not exist yet.
        # The group below is created by Terraform, so this exists only so the
        # runtime does not fail if it tries to create it first.
        Sid      = "CreateLogGroup"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup"]
        Resource = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
      },
      {
        # Token usage and session seconds as custom metrics (PLAN.md 1d).
        # PutMetricData does not support resource-level permissions, so the
        # scope comes from a namespace condition instead of a resource ARN.
        Sid      = "PublishCostMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "PipelineGuard/QAAgent"
          }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "agent" {
  # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); matches the gate log groups.
  name              = "/aws/bedrock-agentcore/${local.name_prefix}"
  retention_in_days = var.log_retention
  kms_key_id        = var.kms_key_arn
}

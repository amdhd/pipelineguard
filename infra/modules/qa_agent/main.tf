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

# --- Code bucket: the agent's deployment zip ---
#
# DELIBERATELY A SEPARATE BUCKET FROM reports.
#
# The reports bucket expires EVERYTHING after 7 days (its lifecycle rule uses an
# empty filter). Agent code parked there would be deleted a week after it started
# working, breaking the runtime with no recent change to blame -- the worst kind
# of failure to diagnose. Sharing one bucket and excluding a prefix would leave
# that rule one careless edit away from doing exactly this. A separate bucket
# makes it impossible by construction.
#
# Versioned, and no expiry: the object version IS the deployment history, and the
# runtime pins one by version id.
resource "aws_s3_bucket" "code" {
  # checkov:skip=CKV_AWS_18:Access logging omitted for a cost-conscious demo -- matches the pipeline artifact bucket.
  # checkov:skip=CKV_AWS_144:Cross-region replication is unnecessary for a rebuildable artefact.
  # checkov:skip=CKV2_AWS_62:No event notifications -- nothing consumes bucket events.
  bucket        = "${local.name_prefix}-code-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # portfolio: allow terraform destroy to clean up
}

resource "aws_s3_bucket_versioning" "code" {
  bucket = aws_s3_bucket.code.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "code" {
  bucket = aws_s3_bucket.code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms" # CKV_AWS_145
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Only incomplete uploads are cleaned up. NOTHING here expires -- see above.
resource "aws_s3_bucket_lifecycle_configuration" "code" {
  bucket = aws_s3_bucket.code.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1 # CKV_AWS_300
    }
    # Keep a few previous versions for rollback, but not forever.
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_public_access_block" "code" {
  bucket                  = aws_s3_bucket.code.id
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
        # AgentCore fetches the deployment zip under this role at cold start.
        # UNVERIFIED until the runtime exists: if the service reads the artifact
        # with its own service principal instead, this statement is unnecessary
        # rather than wrong. Left in because a missing read fails the runtime at
        # startup with an error that points at the artifact, not at IAM.
        Sid      = "ReadAgentCode"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${aws_s3_bucket.code.arn}/*"]
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

# --- GitHub Actions OIDC role for the vesselAI QA workflow ---
#
# The provider is an ACCOUNT-LEVEL SINGLETON created by scripts/bootstrap.sh,
# not by any Terraform stack. IAM allows one per issuer URL per account, and two
# stacks here want one (this and vesselAI's terraform/github-oidc.tf), so
# whichever applied second would fail EntityAlreadyExists and whichever
# destroyed first would silently break the other's role trust. Bootstrap owns
# it; stacks look it up and never manage it. See DISCOVERY.md 1.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_qa" {
  name = "${local.name_prefix}-github"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"

          # ENUMERATED SUBJECTS, StringEquals, never StringLike with a wildcard
          # -- the same discipline vesselAI's own github-oidc.tf argues for.
          # The workflow has three triggers presenting two subjects:
          #   pull_request              -> ...:pull_request
          #   schedule/workflow_dispatch -> ...:ref:refs/heads/main
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.qa_workflow_repo}:pull_request",
            "repo:${var.qa_workflow_repo}:ref:${var.qa_workflow_ref}",
          ]
        }
      }
    }]
  })
}

# CRITICAL, and not enforceable here: the "pull_request" subject above is
# IDENTICAL for a fork PR and a same-repo PR. This trust policy cannot tell them
# apart and does not try to. What keeps a fork out is
#
#   (a) GitHub capping fork-PR permissions at read on a public repo, so
#       id-token: write is never granted -- a platform default, not our control,
#       and one that an admin setting can lift on a private repo; and
#   (b) the explicit fork guard on the credentialed job in ui-qa-agent.yml:
#         github.event.pull_request.head.repo.fork == false
#
# (b) is the control we own. If that guard is ever removed from the workflow,
# this role is reachable by any fork PR that GitHub grants a token to.
resource "aws_iam_role_policy" "github_qa" {
  name = "${local.name_prefix}-github-policy"
  role = aws_iam_role.github_qa.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Invoke the QA runtime -- and nothing else. Name-prefixed rather than a
        # single ARN because an AgentCore runtime ARN carries a
        # server-generated id suffix that is not knowable until creation. This
        # is scoped to this project's runtimes, not to "*".
        Sid    = "InvokeQaRuntime"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime",
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:runtime/${local.name_prefix}-*",
        ]
      },
      {
        # The workflow reads the QA target's credentials to build the health
        # gate's login assertion before the agent is ever invoked.
        Sid      = "ReadQaCredentials"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.qa_target.arn]
      },
      {
        Sid      = "DecryptQaSecret"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn]
      },
    ]
  })
}

# --- AgentCore runtime ---
#
# Created only once a zip exists (see qa_agent_code_key). A runtime cannot
# reference an S3 object that is not there, so the order is: apply everything
# else -> scripts/package-qa-agent.sh -> apply again with the key and version.
# The gate container image has the same constraint; this is that pattern with a
# zip instead of an image.
#
# Schema below was read from `terraform providers schema -json` on the pinned
# provider rather than from documentation. Two things it settles that the docs
# do not make obvious:
#   * lifecycle_configuration is an ATTRIBUTE (list of object), not a block.
#     Writing `lifecycle_configuration { ... }` does not parse.
#   * network_configuration is REQUIRED, even for PUBLIC.
resource "aws_bedrockagentcore_agent_runtime" "qa" {
  count = var.qa_agent_code_key == "" ? 0 : 1

  # UNDERSCORES, not hyphens. The API pattern is [a-zA-Z][a-zA-Z0-9_]{0,47} --
  # this is the one resource in the repo that rejects the hyphenated convention
  # used everywhere else, and it fails at apply, not at plan. Also capped at 48
  # characters starting with a letter; "pipelineguard_qa_dev_runtime" is 28.
  agent_runtime_name = replace("${local.name_prefix}-runtime", "-", "_")
  description        = "UI-QA agent: drives a browser against a deployed app and returns findings JSON"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      runtime = var.agent_runtime_python

      # A COMMAND ARRAY, not [module, function]. Read from the starter toolkit's
      # own packaging code; a docs page found by search claimed the latter and
      # was wrong on every point. ["opentelemetry-instrument", "agent.py"] is
      # the observability variant.
      entry_point = ["agent.py"]

      code {
        s3 {
          bucket = aws_s3_bucket.code.bucket
          prefix = var.qa_agent_code_key
          # Pinning the version makes a deploy immutable: re-running the
          # packaging script does not silently change what a runtime executes.
          version_id = var.qa_agent_code_version_id == "" ? null : var.qa_agent_code_version_id
        }
      }
    }
  }

  # PUBLIC, deliberately. The provider bug that leaves undeletable ENIs and hangs
  # destroy on a dependency cycle is VPC-only -- ENIs exist only there -- and this
  # project tears down as a matter of routine. PUBLIC is also correct on the
  # merits: the agent reaches a public tunnel URL and public AWS APIs, and needs
  # no VPC reachability.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # The one cost control in this design enforced by the PLATFORM rather than by
  # harness logic, so it survives a harness bug or a wedged session. The default
  # idle timeout is 900s and memory bills for every second a session is alive,
  # including idle.
  #
  # BOTH fields must be set together. Specifying max_lifetime alone triggers
  # hashicorp/terraform-provider-aws#45290: AWS computes a default for the
  # omitted idle timeout, Terraform expected null, and the apply fails with
  # "inconsistent result after apply".
  lifecycle_configuration = [{
    idle_runtime_session_timeout = var.idle_session_timeout
    max_lifetime                 = var.max_session_lifetime
  }]

  environment_variables = {
    REPORTS_BUCKET = aws_s3_bucket.reports.bucket
    QA_SECRET_ARN  = aws_secretsmanager_secret.qa_target.arn
    LOG_LEVEL      = "INFO"
  }
}

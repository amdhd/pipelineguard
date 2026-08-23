# --- Secrets: Terraform owns the container, never the material ---
#
# Only the empty secret is managed here. The API keys are seeded out-of-band by
# scripts/seed-gate-secrets.sh and read by the gate Lambdas at runtime.
#
# They are deliberately NOT Terraform variables: `terraform show -json` does not
# redact sensitive values, so anything reaching the plan is written in plaintext
# into plan.json — a pipeline artifact that lands in S3. A
# `data "aws_secretsmanager_secret_version"` lookup leaks the same way, so this
# module never reads the values back either.
resource "aws_secretsmanager_secret" "gate_secrets" {
  # checkov:skip=CKV2_AWS_57:Auto-rotation N/A — these are third-party API keys rotated manually.
  name       = "pipelineguard/gates/${var.environment}"
  kms_key_id = var.kms_key_arn
}

# --- Lambda deployment packages ---
data "archive_file" "cost_gate" {
  type        = "zip"
  source_dir  = "${path.root}/../gates/cost_gate"
  output_path = "${path.module}/build/cost_gate.zip"
}

# --- Lambda layers (binaries built out-of-band by scripts/deploy-gates.sh) ---
# The layer zips are expected under gates/layers/. See docs/runbook.md.
resource "aws_lambda_layer_version" "infracost_binary" {
  layer_name          = "pipelineguard-infracost-${var.environment}"
  filename            = "${path.root}/../gates/layers/infracost_layer.zip"
  compatible_runtimes = ["python3.12"]
  description         = "infracost CLI binary"

  lifecycle {
    # Layer zip is a build artifact; recreate only when it changes.
    ignore_changes = [filename]
  }
}

# The security gate ships Trivy (~160 MB) + Checkov (~160 MB), which together
# blow past Lambda's 250 MB unzipped zip limit, so it is packaged as a container
# image instead. This ECR repo holds that image (built by scripts/deploy-gates.sh).
resource "aws_ecr_repository" "security_gate" {
  # checkov:skip=CKV_AWS_51:Mutable by design — the gate image is re-pushed as :latest on redeploys.
  # checkov:skip=CKV_AWS_136:AES256 at-rest encryption is enabled; a CMK forces repo REPLACEMENT, which would delete the gate image the Lambda runs from.
  # Mutable so the "latest" gate image can be re-pushed on redeploys. (The app
  # image repo, by contrast, is immutable + SHA-tagged per pipeline run.)
  name                 = "pipelineguard-security-gate-${var.environment}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "security_gate" {
  repository = aws_ecr_repository.security_gate.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# --- Cost Gate Lambda ---
resource "aws_lambda_function" "cost_gate" {
  # checkov:skip=CKV_AWS_117:Not in a VPC — the gate needs public egress (Infracost API) and holds no VPC resources.
  # checkov:skip=CKV_AWS_272:Code-signing omitted — source is packaged from this repo by Terraform, not third parties.
  # checkov:skip=CKV_AWS_115:Account concurrency limit is 10; reserving any drops unreserved below the required minimum.
  function_name    = "pipelineguard-cost-gate-${var.environment}"
  filename         = data.archive_file.cost_gate.output_path
  source_code_hash = data.archive_file.cost_gate.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300 # Infracost can take time
  memory_size      = 256
  role             = aws_iam_role.gate_lambda.arn
  kms_key_arn      = var.kms_key_arn # CKV_AWS_173 (env var encryption)

  tracing_config {
    mode = "Active" # CKV_AWS_50 (X-Ray)
  }

  dead_letter_config {
    target_arn = aws_sns_topic.alerts.arn # CKV_AWS_116
  }

  environment {
    variables = {
      SECRETS_ARN    = aws_secretsmanager_secret.gate_secrets.arn
      COST_THRESHOLD = tostring(var.cost_threshold)
      ENVIRONMENT    = var.environment
    }
  }

  layers = [aws_lambda_layer_version.infracost_binary.arn]
}

# --- Security Gate Lambda (container image; Trivy + Checkov too large for zip) ---
resource "aws_lambda_function" "security_gate" {
  # checkov:skip=CKV_AWS_117:Not in a VPC — the gate needs public egress (Trivy DB, Anthropic, GitHub, Slack).
  # checkov:skip=CKV_AWS_272:Code-signing omitted — image is built from this repo by deploy-gates.sh, not third parties.
  # checkov:skip=CKV_AWS_115:Account concurrency limit is 10; reserving any drops unreserved below the required minimum.
  function_name = "pipelineguard-security-gate-${var.environment}"
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.security_gate.repository_url}:${var.security_gate_image_tag}"
  timeout       = 600 # Trivy scan can be slow on large images
  memory_size   = 512
  role          = aws_iam_role.gate_lambda.arn
  kms_key_arn   = var.kms_key_arn # CKV_AWS_173 (env var encryption)

  tracing_config {
    mode = "Active" # CKV_AWS_50 (X-Ray)
  }

  dead_letter_config {
    target_arn = aws_sns_topic.alerts.arn # CKV_AWS_116
  }

  ephemeral_storage {
    size = 2048 # Trivy needs temp space for its vuln DB
  }

  environment {
    variables = {
      SECRETS_ARN = aws_secretsmanager_secret.gate_secrets.arn
      ENVIRONMENT = var.environment
    }
  }
}

# --- Log groups (explicit so retention is enforced) ---
resource "aws_cloudwatch_log_group" "cost_gate" {
  # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); prod uses 30d.
  name              = "/aws/lambda/${aws_lambda_function.cost_gate.function_name}"
  retention_in_days = var.log_retention
  kms_key_id        = var.kms_key_arn
}

resource "aws_cloudwatch_log_group" "security_gate" {
  # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); prod uses 30d.
  name              = "/aws/lambda/${aws_lambda_function.security_gate.function_name}"
  retention_in_days = var.log_retention
  kms_key_id        = var.kms_key_arn
}

# --- IAM role for both gate Lambdas — least privilege ---
resource "aws_iam_role" "gate_lambda" {
  name = "pipelineguard-gate-lambda-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "gate_lambda_policy" {
  # checkov:skip=CKV_AWS_355:"*" is required — GetAuthorizationToken and PutJob* do not support resource scoping.
  name = "pipelineguard-gate-lambda-policy"
  role = aws_iam_role.gate_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read gate API keys (Infracost, Anthropic, Slack).
        Sid      = "ReadGateSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.gate_secrets.arn]
      },
      {
        # Decrypt the KMS-encrypted secret, env vars, and S3 artifacts.
        Sid      = "UseCmk"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
        Resource = [var.kms_key_arn]
      },
      {
        # Deliver failed async invocations to the alerts topic (DLQ).
        Sid      = "PublishDlq"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.alerts.arn]
      },
      {
        # X-Ray tracing.
        Sid      = "XRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = ["*"]
      },
      {
        # Download the terraform plan JSON / terraform files from the artifact bucket.
        Sid      = "ReadWriteArtifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${var.artifact_bucket_arn}/*"]
      },
      {
        # Pull the app image for Trivy scanning.
        Sid    = "ReadEcrForScan"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability"
        ]
        Resource = ["*"] # GetAuthorizationToken requires * resource
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = ["arn:aws:logs:*:*:*"]
      },
      {
        # Signal pass/fail back to the pipeline.
        Sid    = "PipelineJobResult"
        Effect = "Allow"
        Action = [
          "codepipeline:PutJobSuccessResult",
          "codepipeline:PutJobFailureResult"
        ]
        Resource = ["*"] # These actions do not support resource-level scoping
      }
    ]
  })
}

# --- SNS topic for gate/ops alerts ---
resource "aws_sns_topic" "alerts" {
  name              = "pipelineguard-alerts-${var.environment}"
  kms_master_key_id = var.kms_key_arn # CKV_AWS_26
}

# --- Alarm: too many gate failures in one hour ---
resource "aws_cloudwatch_metric_alarm" "gate_failures" {
  alarm_name          = "pipelineguard-gate-failures-${var.environment}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 3
  alarm_description   = "Too many pipeline gate Lambda errors"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.security_gate.function_name
  }
}

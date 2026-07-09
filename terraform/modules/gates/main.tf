data "aws_region" "current" {}

# --- Secrets: store sensitive values in Secrets Manager, never in env vars ---
resource "aws_secretsmanager_secret" "gate_secrets" {
  name = "pipelineguard/gates/${var.environment}"
}

resource "aws_secretsmanager_secret_version" "gate_secrets" {
  secret_id = aws_secretsmanager_secret.gate_secrets.id
  secret_string = jsonencode({
    INFRACOST_API_KEY = var.infracost_api_key
    ANTHROPIC_API_KEY = var.anthropic_api_key
    SLACK_WEBHOOK_URL = var.slack_webhook_url
  })
}

# --- Lambda deployment packages ---
data "archive_file" "cost_gate" {
  type        = "zip"
  source_dir  = "${path.root}/../gates/cost_gate"
  output_path = "${path.module}/build/cost_gate.zip"
}

data "archive_file" "security_gate" {
  type        = "zip"
  source_dir  = "${path.root}/../gates/security_gate"
  output_path = "${path.module}/build/security_gate.zip"
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

resource "aws_lambda_layer_version" "trivy_binary" {
  layer_name          = "pipelineguard-trivy-${var.environment}"
  filename            = "${path.root}/../gates/layers/trivy_layer.zip"
  compatible_runtimes = ["python3.12"]
  description         = "trivy CLI binary + vuln DB cache"

  lifecycle {
    ignore_changes = [filename]
  }
}

resource "aws_lambda_layer_version" "checkov_packages" {
  layer_name          = "pipelineguard-checkov-${var.environment}"
  filename            = "${path.root}/../gates/layers/checkov_layer.zip"
  compatible_runtimes = ["python3.12"]
  description         = "checkov + python deps (anthropic, requests)"

  lifecycle {
    ignore_changes = [filename]
  }
}

# --- Cost Gate Lambda ---
resource "aws_lambda_function" "cost_gate" {
  function_name    = "pipelineguard-cost-gate-${var.environment}"
  filename         = data.archive_file.cost_gate.output_path
  source_code_hash = data.archive_file.cost_gate.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300 # Infracost can take time
  memory_size      = 256
  role             = aws_iam_role.gate_lambda.arn

  environment {
    variables = {
      SECRETS_ARN    = aws_secretsmanager_secret.gate_secrets.arn
      COST_THRESHOLD = tostring(var.cost_threshold)
      ENVIRONMENT    = var.environment
    }
  }

  layers = [aws_lambda_layer_version.infracost_binary.arn]
}

# --- Security Gate Lambda ---
resource "aws_lambda_function" "security_gate" {
  function_name    = "pipelineguard-security-gate-${var.environment}"
  filename         = data.archive_file.security_gate.output_path
  source_code_hash = data.archive_file.security_gate.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 600 # Trivy scan can be slow on large images
  memory_size      = 512
  role             = aws_iam_role.gate_lambda.arn

  ephemeral_storage {
    size = 2048 # Trivy needs temp space for its vuln DB
  }

  environment {
    variables = {
      SECRETS_ARN = aws_secretsmanager_secret.gate_secrets.arn
      ENVIRONMENT = var.environment
    }
  }

  layers = [
    aws_lambda_layer_version.trivy_binary.arn,
    aws_lambda_layer_version.checkov_packages.arn
  ]
}

# --- Log groups (explicit so retention is enforced) ---
resource "aws_cloudwatch_log_group" "cost_gate" {
  name              = "/aws/lambda/${aws_lambda_function.cost_gate.function_name}"
  retention_in_days = var.log_retention
}

resource "aws_cloudwatch_log_group" "security_gate" {
  name              = "/aws/lambda/${aws_lambda_function.security_gate.function_name}"
  retention_in_days = var.log_retention
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
  name = "pipelineguard-alerts-${var.environment}"
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

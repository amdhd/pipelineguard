# Customer-managed KMS key (CMK) used for at-rest encryption across the stack:
# CloudWatch Logs, CodeBuild, CodePipeline artifacts, ECR, Lambda env vars,
# Secrets Manager, SNS, S3, and SSM. Satisfies the Checkov "encrypt with CMK"
# family. One shared key keeps cost to ~$1/month.
data "aws_caller_identity" "current" {}

resource "aws_kms_key" "main" {
  description             = "pipelineguard-${var.environment} CMK for at-rest encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  # Root retains full control (enabling IAM-based grants); AWS services that use
  # the key asynchronously (logs, flow-log delivery, SNS, S3, etc.) are granted
  # use directly since they cannot rely on caller IAM identity.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RootAccountFullAccess"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AllowAwsServicesToUseKey"
        Effect = "Allow"
        Principal = {
          Service = [
            "logs.${var.aws_region}.amazonaws.com",
            "delivery.logs.amazonaws.com",
            "sns.amazonaws.com",
            "s3.amazonaws.com",
            "secretsmanager.amazonaws.com",
            "ecr.amazonaws.com",
            "codepipeline.amazonaws.com",
            "codebuild.amazonaws.com",
            "lambda.amazonaws.com",
            "ssm.amazonaws.com"
          ]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/pipelineguard-${var.environment}"
  target_key_id = aws_kms_key.main.key_id
}

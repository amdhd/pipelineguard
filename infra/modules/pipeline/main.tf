data "aws_caller_identity" "current" {}

locals {
  name_prefix = "pipelineguard-${var.environment}"
}

# --- Artifact bucket (versioning required for pipeline integrity) ---
resource "aws_s3_bucket" "artifacts" {
  # checkov:skip=CKV_AWS_18:Access logging omitted for a cost-conscious demo (would need a second log bucket).
  # checkov:skip=CKV_AWS_144:Cross-region replication is unnecessary for transient CI artifacts.
  # checkov:skip=CKV2_AWS_62:No event notifications — nothing consumes bucket events in this pipeline.
  bucket        = "${local.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # portfolio: allow terraform destroy to clean up
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms" # CKV_AWS_145
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Expire old CI artifacts (CKV2_AWS_61) — keeps the bucket from growing forever.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-artifacts"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7 # CKV_AWS_300
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Expose the ECR repo URL to buildspecs via SSM Parameter Store ---
resource "aws_ssm_parameter" "ecr_repo_url" {
  name   = "/pipelineguard/ecr_repo_url"
  type   = "SecureString" # CKV2_AWS_34
  key_id = var.kms_key_arn
  value  = var.ecr_repo_url
}

# --- GitHub connection (must be authorised once in the console after apply) ---
resource "aws_codestarconnections_connection" "github" {
  name          = substr("pg-${var.environment}", 0, 32)
  provider_type = "GitHub"
}

# --- CloudWatch log group for CodeBuild ---
resource "aws_cloudwatch_log_group" "codebuild" {
  # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); prod uses 30d.
  name              = "/codebuild/${local.name_prefix}"
  retention_in_days = var.log_retention
  kms_key_id        = var.kms_key_arn
}

# =========================================================================
# IAM: CodeBuild role
# =========================================================================
# Keep the existing role identity for Test; it loses all deployment permissions.
resource "aws_iam_role" "codebuild" {
  name = "${local.name_prefix}-codebuild"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.name_prefix}-test" }
      }
    }]
  })
}

resource "aws_iam_role" "codebuild_stage" {
  for_each = toset(["build", "tf_plan", "cost_gate", "security_gate", "deploy"])
  name     = "${local.name_prefix}-codebuild-${replace(each.key, "_", "-")}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.name_prefix}-${replace(each.key, "_", "-")}" }
      }
    }]
  })
}

locals {
  codebuild_roles = merge(
    { test = aws_iam_role.codebuild.id },
    { for stage, role in aws_iam_role.codebuild_stage : stage => role.id }
  )
  layer2_state_bucket = "pipelineguard-tfstate-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  layer2_state_key    = "pipelineguard/layer2/${var.environment}/terraform.tfstate"
  layer2_state_arn    = "arn:aws:s3:::${local.layer2_state_bucket}/${local.layer2_state_key}"
  ecs_service_arn     = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${var.ecs_cluster_name}/${var.ecs_service_name}"
  ecs_task_arn        = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/pipelineguard-app-${var.environment}:*"

  # Both Terraform jobs read the persistent core, but never write or lock it.
  terraform_read_statements = [
    {
      Sid      = "ReadLayer1State"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["arn:aws:s3:::${var.layer1_state_bucket}/${var.layer1_state_key}"]
    },
    {
      Sid      = "ReadLayer2State"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = [local.layer2_state_arn]
    },
    {
      # Backend workspace discovery needs ListBucket; object access is separate
      # and restricted to the default workspace's exact state/lock keys.
      Sid      = "ListStateBuckets"
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = distinct(["arn:aws:s3:::${local.layer2_state_bucket}", "arn:aws:s3:::${var.layer1_state_bucket}"])
    },
    {
      Sid      = "Layer2StateLock"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = ["${local.layer2_state_arn}.tflock"]
    },
    {
      Sid      = "PlanDataSources"
      Effect   = "Allow"
      Action   = ["ec2:DescribeAvailabilityZones"]
      Resource = ["*"]
    }
  ]

  codebuild_stage_statements = {
    build = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = ["*"]
      },
      {
        Sid    = "PushAppImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage", "ecr:PutImage", "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"
        ]
        Resource = [var.ecr_repo_arn]
      },
      {
        Sid      = "ReadEcrUrl"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [aws_ssm_parameter.ecr_repo_url.arn]
      }
    ]
    tf_plan = local.terraform_read_statements
    cost_gate = [{
      Sid      = "InvokeCostGate"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [var.cost_gate_arn]
    }]
    security_gate = [{
      Sid      = "InvokeSecurityGate"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [var.security_gate_arn]
    }]
    deploy = concat(local.terraform_read_statements, [
      {
        Sid      = "WriteLayer2State"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = [local.layer2_state_arn]
      },
      {
        Sid      = "RollAppService"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [local.ecs_service_arn]
      },
      {
        Sid      = "AppTaskRevisions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition"]
        Resource = [local.ecs_task_arn]
      },
      {
        # DescribeTaskDefinition has no resource type in AWS's action table.
        Sid      = "ReadTaskDefinition"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTaskDefinition"]
        Resource = ["*"]
      },
      {
        Sid      = "AppEcsTags"
        Effect   = "Allow"
        Action   = ["ecs:TagResource", "ecs:UntagResource", "ecs:ListTagsForResource"]
        Resource = [local.ecs_task_arn, local.ecs_service_arn]
      },
      {
        Sid    = "PassAppTaskRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/pipelineguard-ecs-execution-${var.environment}",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/pipelineguard-ecs-task-${var.environment}"
        ]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      }
    ])
  }
}

# Preserve the old inline policy's state address as Test's common policy.
moved {
  from = aws_iam_role_policy.codebuild
  to   = aws_iam_role_policy.codebuild["test"]
}

resource "aws_iam_role_policy" "codebuild" {
  for_each = local.codebuild_roles
  name     = "${local.name_prefix}-codebuild-policy"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.codebuild.arn}:*"]
      },
      {
        Sid      = "Artifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket"]
        Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      },
      {
        Sid      = "UseCmk"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "codebuild_stage" {
  for_each = local.codebuild_stage_statements
  # checkov:skip=CKV_AWS_355:Only ECR authorization, AZ discovery and DescribeTaskDefinition require wildcard resources; write actions are scoped.
  name = "${local.name_prefix}-codebuild-${replace(each.key, "_", "-")}-stage"
  role = aws_iam_role.codebuild_stage[each.key].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = each.value
  })
}

# =========================================================================
# IAM: CodePipeline role
# =========================================================================
resource "aws_iam_role" "pipeline" {
  name = "${local.name_prefix}-pipeline"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codepipeline.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "pipeline" {
  # checkov:skip=CKV_AWS_355:"*" is required — StartBuild/InvokeFunction/ListFunctions do not support resource scoping.
  name = "${local.name_prefix}-pipeline-policy"
  role = aws_iam_role.pipeline.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Artifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket"]
        Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      },
      {
        # Read/write the KMS-encrypted artifact store.
        Sid      = "UseCmk"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = [var.kms_key_arn]
      },
      {
        Sid      = "UseGithubConnection"
        Effect   = "Allow"
        Action   = ["codestar-connections:UseConnection"]
        Resource = [aws_codestarconnections_connection.github.arn]
      },
      {
        Sid      = "RunBuilds"
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = ["*"]
      },
      {
        Sid      = "InvokeGates"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction", "lambda:ListFunctions"]
        Resource = ["*"]
      }
    ]
  })
}

# =========================================================================
# CodeBuild projects
# =========================================================================
locals {
  common_env = [
    { name = "AWS_DEFAULT_REGION", value = var.aws_region },
    { name = "ENVIRONMENT", value = var.environment },
    { name = "ECR_REPO_URL", value = var.ecr_repo_url },
    { name = "GITHUB_REPO", value = var.github_repo }
  ]
}

resource "aws_codebuild_project" "test" {
  name           = "${local.name_prefix}-test"
  service_role   = aws_iam_role.codebuild.arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {
      for_each = local.common_env
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-test.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

resource "aws_codebuild_project" "build" {
  # checkov:skip=CKV_AWS_316:Privileged mode is required to run `docker build` for the app image.
  name           = "${local.name_prefix}-build"
  service_role   = aws_iam_role.codebuild_stage["build"].arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true # Docker build
    dynamic "environment_variable" {
      for_each = local.common_env
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-build.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

resource "aws_codebuild_project" "tf_plan" {
  name           = "${local.name_prefix}-tf-plan"
  service_role   = aws_iam_role.codebuild_stage["tf_plan"].arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {
      for_each = local.common_env
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-tf-plan.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

resource "aws_codebuild_project" "cost_gate" {
  name           = "${local.name_prefix}-cost-gate"
  service_role   = aws_iam_role.codebuild_stage["cost_gate"].arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {
      for_each = concat(local.common_env, [
        { name = "COST_GATE_FUNCTION", value = var.cost_gate_name },
        { name = "ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.bucket }
      ])
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-cost-gate.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

resource "aws_codebuild_project" "security_gate" {
  name           = "${local.name_prefix}-security-gate"
  service_role   = aws_iam_role.codebuild_stage["security_gate"].arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {
      for_each = concat(local.common_env, [
        { name = "SECURITY_GATE_FUNCTION", value = var.security_gate_name },
        { name = "ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.bucket }
      ])
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-security-gate.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

resource "aws_codebuild_project" "deploy" {
  name           = "${local.name_prefix}-deploy"
  service_role   = aws_iam_role.codebuild_stage["deploy"].arn
  encryption_key = var.kms_key_arn # CKV_AWS_147

  artifacts { type = "CODEPIPELINE" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false
    dynamic "environment_variable" {
      for_each = concat(local.common_env, [
        { name = "ECS_CLUSTER", value = var.ecs_cluster_name },
        { name = "ECS_SERVICE", value = var.ecs_service_name }
      ])
      content {
        name  = environment_variable.value.name
        value = environment_variable.value.value
      }
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspecs/buildspec-deploy.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

# =========================================================================
# CodePipeline
# =========================================================================
resource "aws_codepipeline" "main" {
  name     = local.name_prefix
  role_arn = aws_iam_role.pipeline.arn

  artifact_store {
    location = aws_s3_bucket.artifacts.bucket
    type     = "S3"

    encryption_key {
      id   = var.kms_key_arn # CKV_AWS_219
      type = "KMS"
    }
  }

  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source_output"]
      configuration = {
        ConnectionArn    = aws_codestarconnections_connection.github.arn
        FullRepositoryId = var.github_repo
        BranchName       = var.github_branch
      }
    }
  }

  stage {
    name = "BuildAndTest"

    action {
      name             = "Test"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["source_output"]
      output_artifacts = ["test_output"]
      run_order        = 1
      configuration    = { ProjectName = aws_codebuild_project.test.name }
    }

    action {
      name             = "Build"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["source_output"]
      output_artifacts = ["build_output"]
      run_order        = 1
      configuration    = { ProjectName = aws_codebuild_project.build.name }
    }
  }

  stage {
    name = "TerraformPlan"
    action {
      name             = "Plan"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["build_output"]
      output_artifacts = ["plan_output"]
      configuration    = { ProjectName = aws_codebuild_project.tf_plan.name }
    }
  }

  stage {
    name = "CostGate"
    action {
      name            = "CostGate"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["plan_output"]
      configuration   = { ProjectName = aws_codebuild_project.cost_gate.name }
    }
  }

  stage {
    name = "SecurityGate"
    action {
      name            = "SecurityGate"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["build_output"]
      configuration   = { ProjectName = aws_codebuild_project.security_gate.name }
    }
  }

  dynamic "stage" {
    for_each = var.enable_manual_approval ? [1] : []
    content {
      name = "Approval"
      action {
        name     = "ManualApproval"
        category = "Approval"
        owner    = "AWS"
        provider = "Manual"
        version  = "1"
      }
    }
  }

  stage {
    name = "Deploy"
    action {
      name            = "Deploy"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["build_output"]
      configuration   = { ProjectName = aws_codebuild_project.deploy.name }
    }
  }
}

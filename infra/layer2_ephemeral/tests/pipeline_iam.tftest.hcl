# No AWS calls: even the test's virtual apply uses only the mock provider.
# These assertions inspect the rendered policies, not copies of intended IAM.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::dummy-artifacts" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-southeast-1:111122223333:log-group:dummy" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/dummy" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:ap-southeast-1:111122223333:parameter/pipelineguard/ecr_repo_url" }
  }
  mock_resource "aws_codestarconnections_connection" {
    defaults = { arn = "arn:aws:codestar-connections:ap-southeast-1:111122223333:connection/00000000-0000-0000-0000-000000000000" }
  }
}

run "stage_permissions" {
  module {
    source = "../modules/pipeline"
  }
  variables {
    environment         = "dev"
    aws_region          = "ap-southeast-1"
    github_repo         = "dummy/repo"
    github_branch       = "main"
    ecr_repo_url        = "111122223333.dkr.ecr.ap-southeast-1.amazonaws.com/dummy-app"
    ecr_repo_arn        = "arn:aws:ecr:ap-southeast-1:111122223333:repository/dummy-app"
    ecs_cluster_name    = "dummy-cluster"
    ecs_service_name    = "dummy-service"
    cost_gate_arn       = "arn:aws:lambda:ap-southeast-1:111122223333:function:dummy-cost"
    cost_gate_name      = "dummy-cost"
    security_gate_arn   = "arn:aws:lambda:ap-southeast-1:111122223333:function:dummy-security"
    security_gate_name  = "dummy-security"
    kms_key_arn         = "arn:aws:kms:ap-southeast-1:111122223333:key/00000000-0000-0000-0000-000000000000"
    layer1_state_bucket = "dummy-persistent-state"
    layer1_state_key    = "pipelineguard/layer1/dev/terraform.tfstate"
  }

  # Give the mock roles distinct identities so swapped project bindings fail.
  override_resource {
    target = aws_iam_role.codebuild
    values = { arn = "arn:aws:iam::111122223333:role/test", id = "test" }
  }
  override_resource {
    target = aws_iam_role.codebuild_stage["build"]
    values = { arn = "arn:aws:iam::111122223333:role/build", id = "build" }
  }
  override_resource {
    target = aws_iam_role.codebuild_stage["tf_plan"]
    values = { arn = "arn:aws:iam::111122223333:role/plan", id = "plan" }
  }
  override_resource {
    target = aws_iam_role.codebuild_stage["cost_gate"]
    values = { arn = "arn:aws:iam::111122223333:role/cost", id = "cost" }
  }
  override_resource {
    target = aws_iam_role.codebuild_stage["security_gate"]
    values = { arn = "arn:aws:iam::111122223333:role/security", id = "security" }
  }
  override_resource {
    target = aws_iam_role.codebuild_stage["deploy"]
    values = { arn = "arn:aws:iam::111122223333:role/deploy", id = "deploy" }
  }

  assert {
    condition = (
      aws_codebuild_project.test.service_role == aws_iam_role.codebuild.arn &&
      aws_codebuild_project.build.service_role == aws_iam_role.codebuild_stage["build"].arn &&
      aws_codebuild_project.tf_plan.service_role == aws_iam_role.codebuild_stage["tf_plan"].arn &&
      aws_codebuild_project.cost_gate.service_role == aws_iam_role.codebuild_stage["cost_gate"].arn &&
      aws_codebuild_project.security_gate.service_role == aws_iam_role.codebuild_stage["security_gate"].arn &&
      aws_codebuild_project.deploy.service_role == aws_iam_role.codebuild_stage["deploy"].arn &&
      alltrue([for stage, policy in aws_iam_role_policy.codebuild : policy.role == local.codebuild_roles[stage]]) &&
      alltrue([for stage, policy in aws_iam_role_policy.codebuild_stage : policy.role == aws_iam_role.codebuild_stage[stage].id])
    )
    error_message = "Every project and inline policy must bind to its own stage role."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild["test"].policy).Statement :
      alltrue([for action in statement.Action : contains([
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
        "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket",
        "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"
        ], action)]) && alltrue([for resource in statement.Resource : contains([
        "arn:aws:s3:::dummy-artifacts", "arn:aws:s3:::dummy-artifacts/*",
        "arn:aws:logs:ap-southeast-1:111122223333:log-group:dummy:*", var.kms_key_arn
      ], resource)])
    ]) && !contains(keys(aws_iam_role_policy.codebuild_stage), "test")
    error_message = "Test must deny ECS updates, PassRole, state access and KMS administration."
  }
  assert {
    condition = alltrue(flatten([
      for stage, policy in aws_iam_role_policy.codebuild_stage : [
        for statement in jsondecode(policy.policy).Statement :
        alltrue([for action in statement.Action : action != "*" && !endswith(action, ":*")]) &&
        (!contains(statement.Resource, "*") || alltrue([
          for action in statement.Action : contains(["ecr:GetAuthorizationToken", "ec2:DescribeAvailabilityZones", "ecs:DescribeTaskDefinition"], action)
        ]))
      ]
    ]))
    error_message = "Wildcard resources must be confined to AWS actions with no supported resource type."
  }
  assert {
    condition = alltrue(flatten([
      for stage, policy in aws_iam_role_policy.codebuild_stage : [
        for statement in jsondecode(policy.policy).Statement :
        alltrue([for action in statement.Action : !startswith(action, "kms:")]) &&
        (stage == "deploy" || alltrue([for action in statement.Action : !startswith(action, "ecs:") && action != "iam:PassRole"]))
      ]
    ]))
    error_message = "Only Deploy may use ECS or PassRole; no stage-specific policy may administer KMS."
  }
  assert {
    condition = alltrue(flatten([
      for stage in ["tf_plan", "deploy"] : [
        for statement in jsondecode(aws_iam_role_policy.codebuild_stage[stage].policy).Statement :
        !contains(statement.Action, "s3:PutObject") && !contains(statement.Action, "s3:DeleteObject") ||
        (length(statement.Resource) == 1 && contains([
          "arn:aws:s3:::pipelineguard-tfstate-111122223333-ap-southeast-1/pipelineguard/layer2/dev/terraform.tfstate.tflock",
          stage == "deploy" ? "arn:aws:s3:::pipelineguard-tfstate-111122223333-ap-southeast-1/pipelineguard/layer2/dev/terraform.tfstate" : "unused"
        ], statement.Resource[0]))
      ]
    ]))
    error_message = "Layer1 state writes, unrelated state keys and persistent locks must be denied."
  }
  assert {
    condition = alltrue([
      for stage in ["tf_plan", "deploy"] : anytrue([
        for statement in jsondecode(aws_iam_role_policy.codebuild_stage[stage].policy).Statement :
        statement.Action == ["s3:GetObject"] && statement.Resource == ["arn:aws:s3:::dummy-persistent-state/pipelineguard/layer1/dev/terraform.tfstate"]
      ])
      ]) && anytrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["deploy"].policy).Statement :
      contains(statement.Action, "ecs:UpdateService") && statement.Resource == ["arn:aws:ecs:ap-southeast-1:111122223333:service/dummy-cluster/dummy-service"]
      ]) && anytrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["deploy"].policy).Statement :
      statement.Action == ["s3:PutObject"] && statement.Resource == ["arn:aws:s3:::pipelineguard-tfstate-111122223333-ap-southeast-1/pipelineguard/layer2/dev/terraform.tfstate"]
    ])
    error_message = "Positive controls: remote-state reads, the intended ECS rollout and layer2 state update must remain allowed."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["build"].policy).Statement :
      alltrue([for action in statement.Action : !startswith(action, "ecr:") || action == "ecr:GetAuthorizationToken"]) || statement.Resource == [var.ecr_repo_arn]
      ]) && alltrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["deploy"].policy).Statement :
      !contains(statement.Action, "iam:PassRole") || (
        statement.Resource == [
          "arn:aws:iam::111122223333:role/pipelineguard-ecs-execution-dev",
          "arn:aws:iam::111122223333:role/pipelineguard-ecs-task-dev"
        ] && try(statement.Condition.StringEquals["iam:PassedToService"], "") == "ecs-tasks.amazonaws.com"
      )
    ])
    error_message = "Image push and task-role passing must be confined to the app resources."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["deploy"].policy).Statement :
      !contains(statement.Action, "ecs:UpdateService") || statement.Resource == ["arn:aws:ecs:ap-southeast-1:111122223333:service/dummy-cluster/dummy-service"]
      ]) && alltrue([
      for statement in jsondecode(aws_iam_role_policy.codebuild_stage["deploy"].policy).Statement :
      !contains(statement.Action, "ecs:RegisterTaskDefinition") || statement.Resource == ["arn:aws:ecs:ap-southeast-1:111122223333:task-definition/pipelineguard-app-dev:*"]
    ])
    error_message = "ECS rollout writes must not extend to another service or task family."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.codebuild.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:codebuild:ap-southeast-1:111122223333:project/pipelineguard-dev-test" &&
      alltrue([for stage, role in aws_iam_role.codebuild_stage :
        jsondecode(role.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:codebuild:ap-southeast-1:111122223333:project/pipelineguard-dev-${replace(stage, "_", "-")}" &&
        jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "111122223333"
      ])
    )
    error_message = "A stage role must trust only its matching CodeBuild project and account."
  }
  assert {
    condition = alltrue([
      for stage in ["cost_gate", "security_gate"] : jsondecode(aws_iam_role_policy.codebuild_stage[stage].policy).Statement[0].Resource == [stage == "cost_gate" ? var.cost_gate_arn : var.security_gate_arn]
      ]) && alltrue([
      for stage in keys(local.codebuild_roles) : anytrue([
        for statement in jsondecode(aws_iam_role_policy.codebuild[stage].policy).Statement :
        contains(statement.Action, "kms:Decrypt") && statement.Resource == [var.kms_key_arn]
      ])
    ])
    error_message = "Gate invocation stays stage-specific and encrypted artifact access remains available."
  }
}

# LAYER 2 — the demo/live stack (networking + ECR + ECS + pipeline + gates).
# Exists only while demoing or live. Bring it up with scripts/demo-up.sh, tear it
# down with scripts/demo-down.sh. While it is down it bills ~$0.
#
# This is the layer that held the ~$50/mo idle burn (NAT + ALB are hourly-rate
# and cannot scale to zero). It is in its OWN state, so destroying it can never
# touch the QA core in layer1_persistent — the runtime-destroy hazard that
# routine demo teardown used to carry is gone by construction.
#
# The QA agent (layer1) is deliberately NOT here: it is PUBLIC-mode (no VPC),
# independent of this VPC/NAT, and keeps working while this layer is destroyed.

data "terraform_remote_state" "layer1" {
  backend = "s3"
  config = {
    bucket  = var.layer1_state_bucket
    key     = var.layer1_state_key
    region  = var.layer1_state_region
    encrypt = true
  }
}

module "networking" {
  source      = "../modules/networking"
  environment = var.environment
  kms_key_arn = data.terraform_remote_state.layer1.outputs.kms_key_arn
}

module "ecr" {
  source      = "../modules/ecr"
  environment = var.environment
}

module "ecs" {
  source          = "../modules/ecs"
  environment     = var.environment
  vpc_id          = module.networking.vpc_id
  private_subnets = module.networking.private_subnet_ids
  public_subnets  = module.networking.public_subnet_ids
  ecr_repo_url    = module.ecr.repository_url
  app_image_tag   = var.app_image_tag
  app_port        = var.app_port
  cpu             = var.ecs_cpu
  memory          = var.ecs_memory
  desired_count   = var.ecs_desired_count
  log_retention   = var.log_retention_days
  kms_key_arn     = data.terraform_remote_state.layer1.outputs.kms_key_arn
}

# The pipeline module owns the artifact bucket, so gates depends on it for the ARN.
module "pipeline" {
  source                 = "../modules/pipeline"
  environment            = var.environment
  aws_region             = var.aws_region
  github_repo            = var.github_repo
  github_branch          = var.github_branch
  ecr_repo_url           = module.ecr.repository_url
  ecs_cluster_name       = module.ecs.cluster_name
  ecs_service_name       = module.ecs.service_name
  cost_gate_arn          = module.gates.cost_gate_arn
  cost_gate_name         = module.gates.cost_gate_name
  security_gate_arn      = module.gates.security_gate_arn
  security_gate_name     = module.gates.security_gate_name
  enable_manual_approval = var.enable_manual_approval
  log_retention          = var.log_retention_days
  kms_key_arn            = data.terraform_remote_state.layer1.outputs.kms_key_arn
}

module "gates" {
  source              = "../modules/gates"
  environment         = var.environment
  cost_threshold      = var.cost_gate_threshold
  artifact_bucket_arn = module.pipeline.artifact_bucket_arn
  log_retention       = var.log_retention_days
  kms_key_arn         = data.terraform_remote_state.layer1.outputs.kms_key_arn
}

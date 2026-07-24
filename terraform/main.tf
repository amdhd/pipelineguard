module "networking" {
  source      = "./modules/networking"
  environment = var.environment
  kms_key_arn = aws_kms_key.main.arn
}

module "ecr" {
  source      = "./modules/ecr"
  environment = var.environment
}

module "ecs" {
  source          = "./modules/ecs"
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
  kms_key_arn     = aws_kms_key.main.arn
}

# The pipeline module owns the artifact bucket, so gates depends on it for the ARN.
module "pipeline" {
  source                 = "./modules/pipeline"
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
  kms_key_arn            = aws_kms_key.main.arn
}

module "gates" {
  source              = "./modules/gates"
  environment         = var.environment
  cost_threshold      = var.cost_gate_threshold
  slack_webhook_url   = var.slack_webhook_url
  infracost_api_key   = var.infracost_api_key
  anthropic_api_key   = var.anthropic_api_key
  github_token        = var.github_token
  artifact_bucket_arn = module.pipeline.artifact_bucket_arn
  log_retention       = var.log_retention_days
  kms_key_arn         = aws_kms_key.main.arn
}

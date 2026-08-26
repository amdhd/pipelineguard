terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Lower-bounded, not "~> 6.0". The AgentCore resources the QA agent needs
      # (aws_bedrockagentcore_agent_runtime) landed in 6.18.0, and "~> 6.0" would
      # happily resolve 6.0.0 and then fail on an unknown resource type -- a
      # confusing error to debug. Upper-bounded at the next major so a v7
      # release cannot be picked up unreviewed.
      version = ">= 6.18, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  backend "s3" {
    # S3-native locking: Terraform writes a "<key>.tflock" object alongside the
    # state and deletes it on release. This replaces the DynamoDB lock table --
    # the "dynamodb_table" backend parameter is deprecated in favour of it, and
    # keeping both configured meant acquiring two locks to protect one state.
    #
    # The role that runs Terraform therefore needs s3:DeleteObject on the lock
    # object, not just Get/Put. Without it a lock is acquired and never
    # released, and every later run fails to lock. See the TerraformStateBackend
    # statement in modules/pipeline/main.tf.
    use_lockfile = true
    # Values provided via backend config file or env vars:
    #   terraform init -backend-config=backend.conf
    # backend.conf should contain: bucket, key, region, encrypt
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "pipelineguard"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner_tag
    }
  }
}

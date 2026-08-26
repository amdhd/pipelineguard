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
    use_lockfile = true
    # Values provided via backend config file or env vars:
    #   terraform init -backend-config=backend.conf
    # backend.conf should contain: bucket, key, region, dynamodb_table, encrypt
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

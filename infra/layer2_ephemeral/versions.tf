# LAYER 2 (ephemeral demo/live stack) — apply → demo → destroy. State lives in
# its own backend object, separate from layer1, so a demo-down can never touch
# the QA core.
terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Same pins as layer1_persistent — keep the two roots on one provider
      # version so plans are comparable.
      version = ">= 6.18, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  backend "s3" {
    # S3-native locking: Terraform writes a "<key>.tflock" object alongside the
    # state and deletes it on release.
    use_lockfile = true
    # Values provided via backend config file or env vars:
    #   terraform init -backend-config=backend.conf
    # backend.conf should contain: bucket, key, region, encrypt
    # key = "pipelineguard/layer2/dev/terraform.tfstate"
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

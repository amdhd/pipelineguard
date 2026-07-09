variable "environment" {
  description = "Deployment environment"
  type        = string
}

resource "aws_ecr_repository" "app" {
  name                 = "pipelineguard-app-${var.environment}"
  image_tag_mutability = "IMMUTABLE" # Immutable tags = auditability + trivial rollback

  image_scanning_configuration {
    scan_on_push = true # ECR native scanning, complements Trivy in the gate
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

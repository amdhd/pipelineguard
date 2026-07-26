data "aws_region" "current" {}

resource "aws_ecs_cluster" "main" {
  name = "pipelineguard-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = "pipelineguard-app-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name         = "app"
    image        = "${var.ecr_repo_url}:${var.app_image_tag}"
    essential    = true
    portMappings = [{ containerPort = var.app_port, protocol = "tcp" }]
    environment = [
      { name = "NODE_ENV", value = var.environment },
      { name = "PORT", value = tostring(var.app_port) }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "app"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "wget -q -O- http://localhost:${var.app_port}/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

resource "aws_ecs_service" "app" {
  name            = "pipelineguard-app-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnets
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.app_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true # Auto-rollback on failed deploy — self-healing deploys
  }

  depends_on = [aws_lb_listener.http]
}

# --- ALB ---
resource "aws_lb" "app" {
  name               = "pipelineguard-alb-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  subnets            = var.public_subnets
  security_groups    = [aws_security_group.alb.id]
  # checkov:skip=CKV_AWS_150:Deletion protection off by design — dev stack is torn down between demos.
  # checkov:skip=CKV_AWS_91:Access logging omitted for a cost-conscious demo (needs a dedicated log bucket).
  # checkov:skip=CKV2_AWS_28:No WAF — demo app on a plain public ALB, no custom domain.
  # checkov:skip=CKV2_AWS_20:No HTTP->HTTPS redirect — HTTP-only demo (no ACM cert/custom domain).
  drop_invalid_header_fields = true # CKV_AWS_131
}

resource "aws_lb_target_group" "app" {
  # checkov:skip=CKV_AWS_378:HTTP target protocol — internal ALB->task hop on a demo app, no TLS termination in-cluster.
  name        = "pipelineguard-tg-${var.environment}"
  port        = var.app_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
  }
}

resource "aws_lb_listener" "http" {
  # checkov:skip=CKV_AWS_2:HTTP listener — demo has no ACM cert/custom domain for HTTPS.
  # checkov:skip=CKV_AWS_103:TLS 1.2 N/A without an HTTPS listener (see above).
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# --- Security Groups ---
resource "aws_security_group" "alb" {
  # checkov:skip=CKV_AWS_260:Public web app — port 80 from 0.0.0.0/0 is the intended entrypoint.
  # checkov:skip=CKV_AWS_382:Open egress — ALB forwards to tasks; scoping adds no value on a demo.
  name        = "pipelineguard-alb-${var.environment}"
  description = "ALB ingress from the internet"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs_tasks" {
  # checkov:skip=CKV_AWS_382:Open egress needed for ECR image pulls + AWS APIs via NAT.
  name        = "pipelineguard-ecs-${var.environment}"
  description = "ECS tasks - only reachable from the ALB"
  vpc_id      = var.vpc_id

  ingress {
    description     = "App port from ALB only"
    from_port       = var.app_port
    to_port         = var.app_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All outbound (image pulls, NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- CloudWatch Logs ---
resource "aws_cloudwatch_log_group" "ecs" {
  # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); prod uses 30d.
  name              = "/ecs/pipelineguard-${var.environment}"
  retention_in_days = var.log_retention
  kms_key_id        = var.kms_key_arn
}

# --- IAM Roles ---
resource "aws_iam_role" "ecs_execution" {
  name = "pipelineguard-ecs-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Execution role: pull image from ECR + write container logs.
resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Decrypt the KMS-encrypted app image (ECR) at pull time.
resource "aws_iam_role_policy" "ecs_execution_kms" {
  name = "pipelineguard-ecs-execution-kms-${var.environment}"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource = [var.kms_key_arn]
    }]
  })
}

# Task role: the app itself needs no AWS permissions (least privilege).
resource "aws_iam_role" "ecs_task" {
  name = "pipelineguard-ecs-task-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# --- CloudWatch Alarms ---
resource "aws_cloudwatch_metric_alarm" "ecs_cpu" {
  alarm_name          = "pipelineguard-ecs-cpu-${var.environment}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "ECS service CPU utilisation is high"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.app.name
  }
}

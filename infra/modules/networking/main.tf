# VPC with 2 public + 2 private subnets across 2 AZs.
# Single NAT Gateway (cost-conscious for a portfolio; prod would use one per AZ).

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "pipelineguard-${var.environment}" }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet("10.0.0.0/16", 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  # The ALB gets its own public IPs; instances never launch here, so no
  # auto-assign public IP is needed (CKV_AWS_130).
  map_public_ip_on_launch = false

  tags = { Name = "pipelineguard-public-${count.index}-${var.environment}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet("10.0.0.0/16", 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "pipelineguard-private-${count.index}-${var.environment}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pipelineguard-igw-${var.environment}" }
}

resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.main]
  tags       = { Name = "pipelineguard-nat-${var.environment}" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
  tags          = { Name = "pipelineguard-nat-${var.environment}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "pipelineguard-public-rt-${var.environment}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "pipelineguard-private-rt-${var.environment}" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Lock the VPC's default security group down to no ingress/egress (CKV2_AWS_12).
# Nothing uses it — the app SGs are explicit — but leaving it open is flagged.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pipelineguard-default-locked-${var.environment}" }
}

# --- VPC flow logs -> CloudWatch (CKV2_AWS_11) ---
resource "aws_cloudwatch_log_group" "flow" {
  name              = "/vpc/flow/pipelineguard-${var.environment}"
  retention_in_days = 7 # checkov:skip=CKV_AWS_338:Short dev retention is intentional (cost); prod uses 30d
  kms_key_id        = var.kms_key_arn
}

resource "aws_iam_role" "flow_log" {
  name = "pipelineguard-flowlog-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "flow_log" {
  name = "pipelineguard-flowlog-${var.environment}"
  role = aws_iam_role.flow_log.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow.arn}:*"
    }]
  })
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow.arn
  iam_role_arn    = aws_iam_role.flow_log.arn
}

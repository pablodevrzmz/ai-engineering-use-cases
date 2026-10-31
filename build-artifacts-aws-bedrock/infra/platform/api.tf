# The Session API: one Fargate task behind an internet-facing ALB. It serves
# the web page, verifies Cognito tokens, keeps threads, relays the agent's SSE
# stream and signs download links.

data "aws_ecr_repository" "api" {
  name = "${var.name}/api"
}

data "aws_ecr_image" "api" {
  repository_name = data.aws_ecr_repository.api.name
  image_tag       = var.image_tag
}

locals {
  use_default_vpc = var.vpc_id == null
  api_image       = "${data.aws_ecr_repository.api.repository_url}:${data.aws_ecr_image.api.image_tag}"
  api_port        = 8000

  api_env = {
    AWS_REGION              = local.region
    COGNITO_USER_POOL_ID    = aws_cognito_user_pool.this.id
    COGNITO_CLIENT_ID       = aws_cognito_user_pool_client.web.id
    THREADS_TABLE           = aws_dynamodb_table.threads.name
    THREAD_TTL_DAYS         = tostring(var.thread_ttl_days)
    FILES_BUCKET            = aws_s3_bucket.files.bucket
    FILES_TTL_DAYS          = tostring(var.files_ttl_days)
    FILES_URL_TTL_SECONDS   = tostring(var.files_url_ttl_seconds)
    AGENT_RUNTIME_ARN       = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
    AGENT_RUNTIME_QUALIFIER = local.endpoint_name
  }
}

# --- Network -----------------------------------------------------------------

data "aws_vpc" "this" {
  default = local.use_default_vpc ? true : null
  id      = var.vpc_id
}

# Default VPC: every default-for-AZ subnet is public. Own VPC: the subnets you
# listed, looked up so a typo fails at plan.
data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.this.id]
  }

  dynamic "filter" {
    for_each = local.use_default_vpc ? [1] : []
    content {
      name   = "default-for-az"
      values = ["true"]
    }
  }

  dynamic "filter" {
    for_each = local.use_default_vpc ? [] : [1]
    content {
      name   = "subnet-id"
      values = var.public_subnet_ids
    }
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Internet-facing ALB for the ${var.name} API"
  vpc_id      = data.aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = local.https_enabled ? 1 : 0

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the API task only"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = local.api_port
  to_port                      = local.api_port
}

resource "aws_security_group" "api" {
  name        = "${var.name}-api"
  description = "${var.name} API task"
  vpc_id      = data.aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  description                  = "From the ALB only"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.api_port
  to_port                      = local.api_port
}

# Cognito JWKS, DynamoDB, S3 and AgentCore are all reached over the internet
# (public subnets, public IP, no NAT).
resource "aws_vpc_security_group_egress_rule" "api_all" {
  security_group_id = aws_security_group.api.id
  description       = "AWS APIs over the internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# --- Load balancer -----------------------------------------------------------

resource "aws_lb" "api" {
  name               = "${var.name}-api"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.public.ids

  # An agent turn can be silent for a while (cold microVM, long tool call);
  # the API sends heartbeats, but the ALB must not cut the stream first.
  idle_timeout               = 600
  drop_invalid_header_fields = true

  lifecycle {
    precondition {
      condition     = length(data.aws_subnets.public.ids) >= 2
      error_message = "An ALB needs public subnets in at least two AZs. The default VPC has none here, or public_subnet_ids lists fewer than two: set vpc_id and public_subnet_ids."
    }
  }
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = local.api_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.this.id

  # A single task: let it leave quickly on redeploys.
  deregistration_delay = 15

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Port 80 forwards to the API, or redirects to 443 once a certificate is set.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.https_enabled ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.https_enabled ? 1 : 0

  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- ECS ---------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  # AgentCore only runs arm64 images, so both images are built for arm64 and
  # the API runs on Graviton too: one buildx platform, and it is cheaper.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.api_image
      essential = true

      portMappings = [
        {
          containerPort = local.api_port
          protocol      = "tcp"
        }
      ]

      environment = [for k, v in local.api_env : { name = k, value = v }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "app"
        }
      }

      # The image has no curl; Python is already there.
      healthCheck = {
        command = [
          "CMD-SHELL",
          "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${local.api_port}/health', timeout=3).status == 200 else 1)\"",
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${var.name}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 60

  # A broken image rolls back instead of flapping, and `apply` returns only
  # once the new task is serving.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  wait_for_steady_state = true

  network_configuration {
    subnets          = data.aws_subnets.public.ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true # public subnets, no NAT: the task needs its own address for egress
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.api_port
  }

  # The target group must be attached to a listener before the service can
  # register into it; the task role must be usable before the first task boots.
  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener.https,
    aws_iam_role_policy.api_task,
    aws_iam_role_policy_attachment.api_execution,
  ]
}

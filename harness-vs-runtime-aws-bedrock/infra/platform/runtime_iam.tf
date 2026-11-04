# Execution role the AgentCore Runtime assumes for the agent container (Runtime flavor only).
# Same shape as the harness role minus what the harness needs for its managed image and memory, plus the pull
# permission on our own ECR repository.

data "aws_iam_policy_document" "runtime_trust" {
  count = var.enable_runtime ? 1 : 0

  statement {
    sid     = "AgentCoreAssumeRole"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  count = var.enable_runtime ? 1 : 0

  name               = "${var.name}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_trust[0].json
}

data "aws_iam_policy_document" "runtime" {
  count = var.enable_runtime ? 1 : 0

  statement {
    sid       = "PullImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [data.aws_ecr_repository.agent[0].arn]
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "InvokeModel"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/*",
    ]
  }

  statement {
    sid       = "AutoSubscribeModels"
    actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
    resources = ["*"]
  }

  statement {
    sid       = "RuntimeLogGroups"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "RuntimeLogStreams"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }

  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:*"]
  }

  statement {
    sid = "XRay"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  # The Strands MCP client in the container signs its gateway calls with this role.
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }
}

resource "aws_iam_role_policy" "runtime" {
  count = var.enable_runtime ? 1 : 0

  name   = "runtime"
  role   = aws_iam_role.runtime[0].id
  policy = data.aws_iam_policy_document.runtime[0].json
}

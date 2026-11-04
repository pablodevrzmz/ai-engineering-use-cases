# Execution role the harness assumes: the model, the gateway, its managed memory, and the plumbing AgentCore
# needs to pull the managed image and write traces, logs and metrics. Mirrors the sample policy in the developer
# guide ("Security for AgentCore harness"), trimmed to what this harness is configured to use.

data "aws_iam_policy_document" "harness_trust" {
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

resource "aws_iam_role" "harness" {
  name               = "${var.name}-harness"
  assume_role_policy = data.aws_iam_policy_document.harness_trust.json
}

data "aws_iam_policy_document" "harness" {
  # Cross-region inference profiles (us.amazon.nova-*, global.anthropic.*) resolve to foundation models in several
  # regions, hence the wildcard region on the model ARN.
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

  # Some Bedrock models (Anthropic, Meta, ...) are delivered through an AWS Marketplace subscription that Bedrock
  # creates on the first call. Nova does not need it; the statement keeps `model_id` switchable.
  statement {
    sid       = "AutoSubscribeModels"
    actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
    resources = ["*"]
  }

  # In PUBLIC network mode the harness pulls its managed container image from ECR Public.
  statement {
    sid       = "EcrPublicManagedImage"
    actions   = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]
    resources = ["*"]
  }

  # AgentCore creates the runtime log group on first invoke.
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
    sid       = "LogsResourcePolicy"
    actions   = ["logs:PutResourcePolicy"]
    resources = ["*"]
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

  # The harness runs under an AgentCore workload identity it must be able to fetch tokens for.
  statement {
    sid = "WorkloadIdentity"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default/workload-identity/*",
    ]
  }

  # Managed memory is created by the harness itself, after this role exists, so its id cannot be referenced here.
  dynamic "statement" {
    for_each = var.harness_memory == "managed" ? [1] : []

    content {
      sid = "ManagedMemory"
      actions = [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:GetEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:DeleteEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
      ]
      resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:memory/*"]
    }
  }

  # The only tool: the web-search gateway, called with SigV4.
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }
}

resource "aws_iam_role_policy" "harness" {
  name   = "harness"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.harness.json
}

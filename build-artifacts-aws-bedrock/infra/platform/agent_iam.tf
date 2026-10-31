# Execution role the AgentCore runtime assumes for the agent container.

data "aws_iam_policy_document" "agent_trust" {
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

resource "aws_iam_role" "agent" {
  name               = "${var.name}-agent-runtime"
  assume_role_policy = data.aws_iam_policy_document.agent_trust.json
}

data "aws_iam_policy_document" "agent" {
  statement {
    sid       = "PullImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [data.aws_ecr_repository.agent.arn]
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Cross-region inference profiles (us.amazon.nova-*) resolve to foundation
  # models in several regions, hence the wildcard region on the model ARN.
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

  # Some Bedrock models (Anthropic, Meta, ...) are delivered through an AWS
  # Marketplace subscription that Bedrock creates on the first call. Without
  # these two actions that first call fails with AccessDenied; Nova does not
  # need it, but the statement keeps `model_id` switchable.
  statement {
    sid       = "AutoSubscribeModels"
    actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
    resources = ["*"]
  }

  # The AWS-managed code interpreter lives under the `aws` account.
  statement {
    sid = "CodeInterpreter"
    actions = [
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:GetCodeInterpreterSession",
      "bedrock-agentcore:InvokeCodeInterpreter",
      "bedrock-agentcore:StopCodeInterpreterSession",
    ]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:aws:code-interpreter/aws.codeinterpreter.v1"]
  }

  # publish_file writes; it never reads back or lists.
  statement {
    sid       = "PublishFiles"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.files.arn}/users/*"]
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
}

resource "aws_iam_role_policy" "agent" {
  name   = "runtime"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent.json
}

# Service role the gateway assumes to call the AWS-managed web-search tool on behalf of whichever agent asked.

data "aws_iam_policy_document" "gateway_trust" {
  statement {
    sid     = "AgentCoreGatewayAssumeRole"
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
      values   = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:gateway/*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.name}-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
}

data "aws_iam_policy_document" "gateway" {
  # The gateway's own ARN is not known until it exists, and the role must exist first: hence gateway/*.
  statement {
    sid       = "InvokeGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:gateway/*"]
  }

  # The web-search tool is an AWS-owned resource (account "aws"), billed per query to this account.
  statement {
    sid       = "InvokeWebSearch"
    actions   = ["bedrock-agentcore:InvokeWebSearch"]
    resources = ["arn:aws:bedrock-agentcore:${local.region}:aws:tool/web-search.v1"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "web-search"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

# Two roles for the API task: the execution role ECS uses to pull the image
# and ship logs, and the task role the application code runs with.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    sid     = "EcsTasksAssumeRole"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${local.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "api_execution" {
  name               = "${var.name}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "api_execution" {
  role       = aws_iam_role.api_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "api_task" {
  name               = "${var.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "api_task" {
  # InvokeAgentRuntime is authorised against the runtime and the endpoint.
  statement {
    sid     = "InvokeAgent"
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn,
      aws_bedrockagentcore_agent_runtime_endpoint.live.agent_runtime_endpoint_arn,
    ]
  }

  statement {
    sid = "Threads"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.threads.arn]
  }

  # Head/presign downloads and delete a thread's files; the API never writes
  # objects (the agent does).
  statement {
    sid       = "ThreadFiles"
    actions   = ["s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.files.arn}/users/*"]
  }

  statement {
    sid       = "ListThreadFiles"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.files.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["users/*"]
    }
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "task"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

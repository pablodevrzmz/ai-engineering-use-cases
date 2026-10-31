# The agent: one container image hosted by Amazon Bedrock AgentCore Runtime,
# reached through a named endpoint ("live").

data "aws_ecr_repository" "agent" {
  name = "${var.name}/agent"
}

# Resolving the tag here makes `terraform plan` fail when the image was never
# pushed, instead of AgentCore failing minutes into the apply.
data "aws_ecr_image" "agent" {
  repository_name = data.aws_ecr_repository.agent.name
  image_tag       = var.image_tag
}

locals {
  agent_image = "${data.aws_ecr_repository.agent.repository_url}:${data.aws_ecr_image.agent.image_tag}"
}

resource "aws_bedrockagentcore_agent_runtime" "this" {
  # AgentCore names allow letters, digits and underscores only.
  agent_runtime_name = replace(var.name, "-", "_")
  description        = "Artifacts demo agent (Strands + Code Interpreter + publish_file)"
  role_arn           = aws_iam_role.agent.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_image
    }
  }

  # Plain HTTP contract: POST /invocations streams SSE, GET /ping is the probe.
  protocol_configuration {
    server_protocol = "HTTP"
  }

  # PUBLIC = AgentCore-managed egress. The agent only talks to AWS APIs
  # (Bedrock, Code Interpreter, S3) over their public endpoints, so there is
  # nothing to gain from a VPC here and nothing else to pay for.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # The API forwards the caller's identity in this header; AgentCore drops any
  # custom header that is not allowlisted.
  request_header_configuration {
    request_header_allowlist = ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id"]
  }

  environment_variables = {
    FILES_BUCKET                     = aws_s3_bucket.files.bucket
    FILES_TTL_DAYS                   = tostring(var.files_ttl_days)
    FILES_MAX_BYTES                  = tostring(var.files_max_bytes)
    MODEL_ID                         = var.model_id
    CODE_INTERPRETER_SESSION_SECONDS = tostring(var.code_interpreter_session_seconds)
    # AWS_REGION is not set here on purpose: AgentCore injects the region into the container, and
    # names starting with AWS_ are reserved in several AWS services.
  }

  # Session idle/max lifetime stay at the AgentCore defaults.

  # The first microVM pulls the image with this role; make sure the policy is
  # attached before AgentCore tries.
  depends_on = [aws_iam_role_policy.agent]
}

# Pinned to the runtime version, so pushing a new tag creates a new version and
# moves the endpoint to it in the same apply. The API always calls "live".
resource "aws_bedrockagentcore_agent_runtime_endpoint" "live" {
  name                  = local.endpoint_name
  agent_runtime_id      = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.this.agent_runtime_version
}

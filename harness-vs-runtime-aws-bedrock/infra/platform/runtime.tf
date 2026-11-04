# Flavor B: the same agent as code, hosted by Amazon Bedrock AgentCore Runtime.
#
# Here the agent is a container image (services/agent): a FastAPI server that speaks AgentCore's HTTP contract
# and a Strands Agent whose single tool is the same web-search gateway, reached through an MCP client. You own
# the loop, the framework, the streaming format and the hooks; AgentCore owns the microVMs, sessions and scaling.
# Everything in this file is optional: `enable_runtime = false` deploys the harness alone.

data "aws_ecr_repository" "agent" {
  count = var.enable_runtime ? 1 : 0

  name = "${var.name}/agent"
}

# Resolving the tag here makes `terraform plan` fail when the image was never pushed, instead of AgentCore failing
# minutes into the apply.
data "aws_ecr_image" "agent" {
  count = var.enable_runtime ? 1 : 0

  repository_name = data.aws_ecr_repository.agent[0].name
  image_tag       = var.image_tag
}

locals {
  agent_image = var.enable_runtime ? "${data.aws_ecr_repository.agent[0].repository_url}:${data.aws_ecr_image.agent[0].image_tag}" : null
}

resource "aws_bedrockagentcore_agent_runtime" "this" {
  count = var.enable_runtime ? 1 : 0

  agent_runtime_name = "${local.agent_name}_rt"
  description        = "Research agent as code (Strands + MCP client to the web-search gateway)"
  role_arn           = aws_iam_role.runtime[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_image
    }
  }

  # Plain HTTP contract: POST /invocations streams SSE, GET /ping is the probe.
  protocol_configuration {
    server_protocol = "HTTP"
  }

  # PUBLIC = AgentCore-managed egress; the container only talks to Bedrock and the gateway.
  network_configuration {
    network_mode = "PUBLIC"
  }

  # Same session caps as the harness, so neither flavor outlives the other.
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 600
    max_lifetime                 = 3600
  }]

  environment_variables = {
    MODEL_ID    = var.model_id
    GATEWAY_URL = aws_bedrockagentcore_gateway.this.gateway_url
    # The very same bytes the harness received in system_prompt.text (one file, see variables.tf). AgentCore rejects
    # control characters in environment variables, so the paragraph breaks travel base64-encoded (1.4 KB of the
    # 5 KB a value may hold) and app/settings.py decodes them.
    SYSTEM_PROMPT_B64 = base64encode(local.system_prompt)
    # AWS_REGION is not set here on purpose: AgentCore injects it, and AWS_* names are reserved.
  }

  # The first microVM pulls the image with this role; make sure the policy is attached before AgentCore tries.
  depends_on = [aws_iam_role_policy.runtime]
}

# Pinned to the runtime version, so pushing a new tag creates a new version and moves the endpoint to it in the
# same apply. The client always calls "live".
resource "aws_bedrockagentcore_agent_runtime_endpoint" "live" {
  count = var.enable_runtime ? 1 : 0

  name                  = local.endpoint_name
  agent_runtime_id      = aws_bedrockagentcore_agent_runtime.this[0].agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.this[0].agent_runtime_version
}

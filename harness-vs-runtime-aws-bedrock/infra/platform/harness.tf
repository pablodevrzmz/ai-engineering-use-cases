# Flavor A: the AgentCore harness. This resource IS the agent.
#
# No code, no container, no server: you declare the model, the prompt, the tools, the memory and the limits, and
# AgentCore runs the orchestration loop (powered by Strands Agents) in an isolated microVM per session, with a
# filesystem, a shell, tracing and versioning included. There is no separate charge for the harness; you pay for the
# model calls, the microVM seconds, the memory and the web searches it makes.

resource "aws_bedrockagentcore_harness" "this" {
  harness_name       = local.agent_name
  execution_role_arn = aws_iam_role.harness.arn

  # Identical to the Runtime flavor's BedrockModel(max_tokens=4096, temperature=0.2) in services/agent/app/agent.py.
  model {
    bedrock_model_config {
      model_id    = var.model_id
      max_tokens  = 4096
      temperature = 0.2
    }
  }

  system_prompt {
    text = local.system_prompt
  }

  tool {
    type = "agentcore_gateway"
    name = local.gateway_tool

    config {
      agentcore_gateway {
        gateway_arn = aws_bedrockagentcore_gateway.this.gateway_arn

        outbound_auth {
          aws_iam = true
        }
      }
    }
  }

  # Only the gateway's tools. Without this the built-in `shell` and `file_operations` tools are offered too, which
  # adds about 900 input tokens to every model call and gives the harness two tools the Runtime flavor lacks.
  allowed_tools = ["@${local.gateway_tool}"]

  # PoC guards (service defaults: 75 iterations, 3600 s, no token budget).
  max_iterations  = 20
  timeout_seconds = 300
  max_tokens      = var.harness_max_tokens

  # Attribute, not a block (list of one object); Terraform wants every key of the object, hence summarization = null.
  truncation = [{
    strategy = "sliding_window"
    config = [{
      sliding_window = [{ messages_count = 30 }]
      summarization  = null
    }]
  }]

  # Managed = AgentCore creates (and deletes with the harness) a Memory with short-term events and long-term
  # records; the harness keeps the conversation across invocations of one session either way.
  memory {
    dynamic "managed_memory_configuration" {
      for_each = var.harness_memory == "managed" ? [1] : []

      content {
        strategies            = ["SEMANTIC", "SUMMARIZATION"]
        event_expiry_duration = 7
      }
    }

    dynamic "disabled" {
      for_each = var.harness_memory == "disabled" ? [1] : []
      content {}
    }
  }

  environment {
    agentcore_runtime_environment {
      # PUBLIC = AgentCore-managed egress; the harness only talks to AWS APIs.
      network_configuration {
        network_mode = "PUBLIC"
      }

      # Memory stays billable while a microVM session lives (defaults 900 s idle / 28800 s max): shorter for a PoC.
      lifecycle_configuration = [{
        idle_runtime_session_timeout = 600
        max_lifetime                 = 3600
      }]
    }
  }

  depends_on = [aws_iam_role_policy.harness, aws_bedrockagentcore_gateway_target.web_search]
}

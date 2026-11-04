# The one tool both flavors share: AWS's managed web search, exposed as an MCP tool by an AgentCore Gateway.
#
# Neither flavor talks to a search API directly. The harness lists the gateway as a tool; the Runtime container
# connects to the same gateway URL with an MCP client. Same tool, same results, same bill.

resource "aws_bedrockagentcore_gateway" "this" {
  name        = var.name
  description = "Web search for the harness-vs-runtime demo agents"
  role_arn    = aws_iam_role.gateway.arn

  # SigV4 in: both agents call the gateway with their execution roles, no OAuth server to run for a demo.
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"

  # No semantic tool search: with one tool there is nothing to search, and indexing is billed.

  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "web_search" {
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id
  name               = local.target_name
  description        = "AWS managed Web Search Tool connector"

  # The gateway calls the tool with its own role (gateway_iam.tf), whoever the caller is.
  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      connector {
        source {
          connector_id = "web-search" # version omitted = the connector's current default
        }

        configuration {
          name             = "WebSearch"
          parameter_values = jsonencode({})
          # Target-level domain filtering is one line here, e.g.
          # parameter_values = jsonencode({ domainFilter = { exclude = ["example.com"] } })
        }
      }
    }
  }
}

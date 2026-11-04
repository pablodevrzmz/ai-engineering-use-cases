output "region" {
  value = local.region
}

output "model_id" {
  value = var.model_id
}

output "gateway_url" {
  description = "MCP endpoint of the shared web-search gateway (SigV4)."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "gateway_arn" {
  value = aws_bedrockagentcore_gateway.this.gateway_arn
}

output "harness_arn" {
  description = "Pass to InvokeHarness (scripts/chat.py --flavor harness)."
  value       = aws_bedrockagentcore_harness.this.arn
}

output "harness_id" {
  value = aws_bedrockagentcore_harness.this.harness_id
}

output "harness_runtime_id" {
  description = "Id of the AgentCore Runtime the harness runs on; its log group (/aws/bedrock-agentcore/runtimes/<id>-DEFAULT) is created on first invoke and swept by `make destroy`."
  value       = try(aws_bedrockagentcore_harness.this.environment_actual[0].agentcore_runtime_environment[0].agent_runtime_id, null)
}

output "harness_memory_arn" {
  description = "The managed Memory the harness created (null when harness_memory = disabled)."
  value       = try(aws_bedrockagentcore_harness.this.memory_actual[0].managed_memory_configuration[0].arn, null)
}

output "runtime_arn" {
  description = "Pass to InvokeAgentRuntime with qualifier \"live\" (scripts/chat.py --flavor runtime). null when enable_runtime = false."
  value       = one(aws_bedrockagentcore_agent_runtime.this[*].agent_runtime_arn)
}

output "runtime_id" {
  value = one(aws_bedrockagentcore_agent_runtime.this[*].agent_runtime_id)
}

output "runtime_endpoint_arn" {
  value = one(aws_bedrockagentcore_agent_runtime_endpoint.live[*].agent_runtime_endpoint_arn)
}

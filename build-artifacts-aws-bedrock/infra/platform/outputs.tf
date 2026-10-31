output "api_url" {
  description = "Open this in a browser (with a certificate, point your DNS name at the ALB and use that instead)."
  value       = local.https_enabled ? "https://${aws_lb.api.dns_name}" : "http://${aws_lb.api.dns_name}"
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "demo_user_email" {
  value = aws_cognito_user.demo.username
}

output "files_bucket" {
  value = aws_s3_bucket.files.bucket
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

output "agent_runtime_id" {
  value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
}

output "agent_log_group" {
  description = "Created by AgentCore on the first invoke; not managed by Terraform, delete it by hand after `destroy`."
  value       = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.this.agent_runtime_id}-${local.endpoint_name}"
}

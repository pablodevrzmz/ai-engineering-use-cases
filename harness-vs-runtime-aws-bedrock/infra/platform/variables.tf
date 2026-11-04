variable "region" {
  description = "AWS region for every resource. Must match the ECR stack and offer the Web Search Tool connector."
  type        = string
  default     = "us-east-1"

  # The web-search connector behind the gateway is only offered in these regions (AgentCore developer guide,
  # "Web Search Tool"). Failing here beats a cryptic CreateGatewayTarget error minutes into the apply.
  validation {
    condition     = contains(["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-southeast-2", "ap-northeast-1"], var.region)
    error_message = "region must be one where the AgentCore Web Search Tool is available: us-east-1, us-east-2, us-west-2, eu-west-1, ap-southeast-2, ap-northeast-1."
  }
}

variable "name" {
  description = "Project prefix for every resource name. Kept short because `replace(name, \"-\", \"_\")` becomes the harness name (max 40 chars) and the runtime name."
  type        = string
  default     = "harness-vs-runtime"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.name))
    error_message = "name must be 1-24 chars, lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "model_id" {
  description = "Bedrock model (or cross-region inference profile) BOTH flavors run on, so the comparison is fair. Nova 2 Lite is the cheapest model that streams a tool call against the gateway's WebSearch schema; Nova Pro fails on it (see README)."
  type        = string
  default     = "us.amazon.nova-2-lite-v1:0"
}

variable "enable_runtime" {
  description = "Deploy the Runtime flavor (container on AgentCore Runtime) next to the harness. false = harness only; no Docker, no ECR stack needed."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Tag pushed to the agent ECR repository by `make images`. Required when enable_runtime is true; a tag missing from the repository fails at plan time."
  type        = string
  default     = null

  validation {
    condition     = !var.enable_runtime || (var.image_tag != null && can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.image_tag)))
    error_message = "image_tag is required when enable_runtime is true (run `make images`, or pass IMAGE_TAG=<tag>) and must be a valid container image tag."
  }
}

variable "harness_memory" {
  description = "Harness memory: \"managed\" = AgentCore creates and owns a Memory (short-term events + long-term records, billed); \"disabled\" = conversation state lives only in the session's microVM."
  type        = string
  default     = "managed"

  validation {
    condition     = contains(["managed", "disabled"], var.harness_memory)
    error_message = "harness_memory must be \"managed\" or \"disabled\"."
  }
}

variable "harness_max_tokens" {
  description = "Output-token budget for ONE harness invocation, summed over every model call in it (the harness stops with max_output_tokens_exceeded when reached). A PoC guard, not a per-call limit."
  type        = number
  default     = 60000
}

# Shared lookups and derived values used by several files.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  # AgentCore names allow letters, digits and underscores only.
  agent_name = replace(var.name, "-", "_")

  # ONE prompt, two consumers: the harness gets it through `system_prompt.text`, the Runtime container through the
  # SYSTEM_PROMPT environment variable. Edit the file, `make deploy`, both flavors change. The fairness of the
  # comparison rests on this line.
  system_prompt = file("${path.module}/../../prompts/system-prompt.md")

  endpoint_name = "live"       # the AgentCore Runtime endpoint the client invokes (its qualifier)
  gateway_tool  = "web_search" # the harness's name for its gateway tool entry; allowed_tools refers to it as "@web_search"
  target_name   = "web-search" # the gateway target; the gateway exposes its tool as "web-search___WebSearch"
}

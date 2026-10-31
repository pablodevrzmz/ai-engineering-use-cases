variable "region" {
  description = "AWS region for every resource. Must match the ECR stack."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Project prefix for every resource name. Kept short because `<name>-api` must fit the 32-char ALB limit and `replace(name, \"-\", \"_\")` becomes the AgentCore runtime name."
  type        = string
  default     = "artifacts-demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.name))
    error_message = "name must be 1-24 chars, lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "image_tag" {
  description = "Tag pushed to both ECR repositories (api and agent) by `make images`. A tag that is missing from either repository fails at plan time."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.image_tag))
    error_message = "image_tag must be a valid container image tag."
  }
}

variable "model_id" {
  description = "Bedrock model (or cross-region inference profile) the agent runs on."
  type        = string
  default     = "us.amazon.nova-pro-v1:0"
}

# One number drives the S3 lifecycle rule AND the FILES_TTL_DAYS env of both
# containers, so the expiry shown on a download card is the day the bucket
# actually deletes the object.
variable "files_ttl_days" {
  description = "Days a published file stays in S3 (lifecycle rule) and is advertised as available."
  type        = number
  default     = 30

  validation {
    condition     = var.files_ttl_days >= 1 && floor(var.files_ttl_days) == var.files_ttl_days
    error_message = "files_ttl_days must be a whole number of days, at least 1."
  }
}

variable "files_max_bytes" {
  description = "Largest file the agent will publish from the sandbox (bytes)."
  type        = number
  default     = 15728640
}

variable "files_url_ttl_seconds" {
  description = "Lifetime of the presigned download links the API hands out."
  type        = number
  default     = 300
}

variable "code_interpreter_session_seconds" {
  description = "Code Interpreter session timeout requested by the agent (the AWS TTL is absolute, not idle)."
  type        = number
  default     = 3600
}

variable "thread_ttl_days" {
  description = "Days a thread row stays in DynamoDB before TTL removes it."
  type        = number
  default     = 30
}

variable "demo_user_email" {
  description = "Email (= username) of the demo user Terraform creates in Cognito."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.demo_user_email))
    error_message = "demo_user_email must look like an email address."
  }
}

variable "demo_user_password" {
  description = "Permanent password of the demo user. Mirrors the pool policy so a bad value fails at plan instead of at apply."
  type        = string
  sensitive   = true

  validation {
    condition = alltrue([
      length(var.demo_user_password) >= 12,
      can(regex("[a-z]", var.demo_user_password)),
      can(regex("[A-Z]", var.demo_user_password)),
      can(regex("[0-9]", var.demo_user_password)),
      can(regex("[\\^$*.\\[\\]{}()?\"!@#%&/\\\\,><':;|_~`+=-]", var.demo_user_password)),
    ])
    error_message = "demo_user_password must be at least 12 characters and contain a lowercase letter, an uppercase letter, a digit and one of ^ $ * . [ ] { } ( ) ? \" ! @ # % & / \\ , > < ' : ; | _ ~ ` + = -"
  }
}

variable "vpc_id" {
  description = "VPC for the ALB and the API task. null = the account's default VPC."
  type        = string
  default     = null
}

variable "public_subnet_ids" {
  description = "Public subnets (two or more AZs) when vpc_id is set. Ignored for the default VPC, where every default-for-AZ subnet is used."
  type        = list(string)
  default     = []

  validation {
    condition     = var.vpc_id == null || length(var.public_subnet_ids) >= 2
    error_message = "public_subnet_ids must list at least two subnets when vpc_id is set."
  }
}

variable "acm_certificate_arn" {
  description = "ACM certificate for an HTTPS listener. null = HTTP only (fine for a demo, not for real use)."
  type        = string
  default     = null
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task."
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.api_cpu)
    error_message = "api_cpu must be one of 256, 512, 1024, 2048, 4096."
  }
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API task; must be a valid pairing with api_cpu."
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "Retention of the API log group."
  type        = number
  default     = 14
}

# Shared lookups and derived values used by several files.
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.region
  endpoint_name = "live" # the AgentCore endpoint the API invokes (its qualifier)
  https_enabled = var.acm_certificate_arn != null
}

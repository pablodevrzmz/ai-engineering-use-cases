# Container registry for the Runtime flavor's agent image.
#
# Only the Runtime flavor needs this stack. The harness flavor is configuration: there is no image to build or push.
# It is a separate stack because the platform stack resolves `image_tag` against this repository at plan time; the
# repository (and a pushed image) must exist before the platform can even be planned.

variable "region" {
  description = "AWS region for the registry (same as the platform stack)."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Project prefix; the repository is created as <name>/agent."
  type        = string
  default     = "harness-vs-runtime"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.name))
    error_message = "name must be 1-24 chars, lowercase letters, digits and hyphens, starting with a letter."
  }
}

resource "aws_ecr_repository" "agent" {
  name = "${var.name}/agent"

  # Immutable tags: a deployed tag can never silently change underneath a running runtime version.
  image_tag_mutability = "IMMUTABLE"

  # Demo project: `terraform destroy` removes the repo even when images remain.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "agent_repository_url" {
  description = "Repository URL for the agent image."
  value       = aws_ecr_repository.agent.repository_url
}

output "registry" {
  description = "Registry host (for `docker login`)."
  value       = split("/", aws_ecr_repository.agent.repository_url)[0]
}

# Container registries for the two images (Session API and agent).
# This is a separate stack because the platform stack needs the images to
# exist before it can plan: it resolves `image_tag` against these repos.

variable "region" {
  description = "AWS region for the registries (same as the platform stack)."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Project prefix; repositories are created as <name>/api and <name>/agent."
  type        = string
  default     = "artifacts-demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.name))
    error_message = "name must be 1-24 chars, lowercase letters, digits and hyphens, starting with a letter."
  }
}

locals {
  repositories = toset(["api", "agent"])
}

resource "aws_ecr_repository" "this" {
  for_each = local.repositories

  name = "${var.name}/${each.key}"

  # Immutable tags: a deployed tag can never silently change underneath a
  # running task or runtime version.
  image_tag_mutability = "IMMUTABLE"

  # Demo project: `terraform destroy` removes the repos even when images remain.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = local.repositories

  repository = aws_ecr_repository.this[each.key].name

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

output "api_repository_url" {
  description = "Repository URL for the Session API image."
  value       = aws_ecr_repository.this["api"].repository_url
}

output "agent_repository_url" {
  description = "Repository URL for the agent image."
  value       = aws_ecr_repository.this["agent"].repository_url
}

output "registry" {
  description = "Registry host (for `docker login`)."
  value       = split("/", aws_ecr_repository.this["api"].repository_url)[0]
}

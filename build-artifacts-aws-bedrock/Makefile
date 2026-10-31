# build-artifacts-aws-bedrock: local deploy flow (no CI). Run from this folder with AWS credentials in your
# environment (AWS_PROFILE/AWS_REGION or keys). Prerequisites: AWS CLI, Terraform >= 1.14, Docker with buildx,
# Python 3.13+. See README.md for the full walkthrough.

AWS_REGION   ?= us-east-1
NAME         ?= artifacts-demo
IMAGE_TAG    ?= $(shell date -u +%Y%m%d%H%M%S)
TF           ?= terraform
TF_APPROVE   ?=                     # set to -auto-approve for non-interactive applies
VENV         ?= .venv
PY           ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
ECR_DIR      := infra/ecr
PLATFORM_DIR := infra/platform
TAG_FILE     := $(PLATFORM_DIR)/.image_tag
STACK_VARS   := -var region=$(AWS_REGION) -var name=$(NAME)

# `make deploy` uses the last pushed tag unless IMAGE_TAG is given on the command line.
ifeq ($(origin IMAGE_TAG), command line)
DEPLOY_TAG := $(IMAGE_TAG)
else
DEPLOY_TAG := $(shell cat $(TAG_FILE) 2>/dev/null)
endif

.PHONY: help bootstrap login images deploy outputs smoke venv test validate destroy clean

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

bootstrap:  ## 1. create the two ECR repositories (once per account)
	$(TF) -chdir=$(ECR_DIR) init -input=false
	$(TF) -chdir=$(ECR_DIR) apply -input=false $(TF_APPROVE) $(STACK_VARS)

login:  ## docker login to the account's ECR registry
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $$($(TF) -chdir=$(ECR_DIR) output -raw registry)

images: login  ## 2. build and push both images for linux/arm64 (AgentCore requires ARM64) under IMAGE_TAG
	@echo "IMAGE_TAG=$(IMAGE_TAG)"
	@set -e; REGISTRY=$$($(TF) -chdir=$(ECR_DIR) output -raw registry); \
	for svc in api agent; do \
	  docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
	    -t $$REGISTRY/$(NAME)/$$svc:$(IMAGE_TAG) --push services/$$svc; \
	  docker buildx imagetools inspect $$REGISTRY/$(NAME)/$$svc:$(IMAGE_TAG) | grep -E 'MediaType|Platform' | head -3; \
	done; echo $(IMAGE_TAG) > $(TAG_FILE)

deploy:  ## 3. apply the platform stack (Cognito, DynamoDB, S3, AgentCore runtime, ALB + Fargate API)
	@test -n "$(DEPLOY_TAG)" || { echo 'no image tag: run make images first or pass IMAGE_TAG=<tag>'; exit 1; }
	$(TF) -chdir=$(PLATFORM_DIR) init -input=false
	$(TF) -chdir=$(PLATFORM_DIR) apply -input=false $(TF_APPROVE) $(STACK_VARS) -var image_tag=$(DEPLOY_TAG)
	@echo $(DEPLOY_TAG) > $(TAG_FILE)
	@$(MAKE) --no-print-directory outputs

outputs:  ## print the stack outputs (API URL, Cognito ids, bucket, runtime)
	@$(TF) -chdir=$(PLATFORM_DIR) output

smoke:  ## 4. end to end: sign in, ask the agent for a CSV, download it, delete the thread
	$(PY) scripts/smoke.py --region $(AWS_REGION) \
	  --url $$($(TF) -chdir=$(PLATFORM_DIR) output -raw api_url) \
	  --client-id $$($(TF) -chdir=$(PLATFORM_DIR) output -raw cognito_client_id) \
	  --email $$($(TF) -chdir=$(PLATFORM_DIR) output -raw demo_user_email) \
	  --tfvars $(PLATFORM_DIR)/terraform.tfvars

venv:  ## create a local virtualenv with both services' dependencies and the test tools
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt -r services/api/requirements.txt -r services/agent/requirements.txt

test:  ## run the unit tests of both services (moto, no AWS calls)
	cd services/api && ../../$(VENV)/bin/python -m pytest -q
	cd services/agent && ../../$(VENV)/bin/python -m pytest -q

validate:  ## terraform fmt -check + validate for both stacks (no credentials needed)
	@for d in $(ECR_DIR) $(PLATFORM_DIR); do \
	  $(TF) -chdir=$$d fmt -check -recursive && \
	  $(TF) -chdir=$$d init -backend=false -input=false >/dev/null && \
	  $(TF) -chdir=$$d validate || exit 1; \
	done

destroy:  ## tear everything down (platform, then ECR, then the log groups AgentCore and Container Insights create on their own)
	@set -e; RT=$$($(TF) -chdir=$(PLATFORM_DIR) output -raw agent_runtime_id 2>/dev/null || true); \
	$(TF) -chdir=$(PLATFORM_DIR) destroy -input=false $(TF_APPROVE) $(STACK_VARS) -var image_tag=$(DEPLOY_TAG); \
	$(TF) -chdir=$(ECR_DIR) destroy -input=false $(TF_APPROVE) $(STACK_VARS); \
	for LOG in /aws/bedrock-agentcore/runtimes/$$RT-live /aws/bedrock-agentcore/runtimes/$$RT-DEFAULT /aws/ecs/containerinsights/$(NAME)/performance; do \
	  aws logs delete-log-group --region $(AWS_REGION) --log-group-name "$$LOG" 2>/dev/null && echo "deleted $$LOG" || true; \
	done; rm -f $(TAG_FILE)

clean:  ## remove local build leftovers (virtualenv, pytest caches)
	rm -rf $(VENV) services/*/.pytest_cache services/*/app/__pycache__ services/*/tests/__pycache__

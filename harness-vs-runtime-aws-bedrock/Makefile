# harness-vs-runtime-aws-bedrock: local deploy flow (no CI). Run from this folder with AWS credentials in your
# environment (AWS_PROFILE/AWS_REGION or keys). Prerequisites: AWS CLI, Terraform >= 1.14, Python 3.13+, and for the
# Runtime flavor Docker with buildx. See README.md for the full walkthrough.

AWS_REGION     ?= us-east-1
NAME           ?= harness-vs-runtime
ENABLE_RUNTIME ?= true                # false = deploy the harness alone (no Docker, no ECR stack)
HARNESS_MEMORY ?= managed             # or disabled
MODEL_ID       ?=                     # empty = the Terraform default (us.amazon.nova-pro-v1:0); both flavors use it
IMAGE_TAG      ?= $(shell date -u +%Y%m%d%H%M%S)
FLAVOR         ?= harness             # make chat FLAVOR=harness|runtime
ONCE           ?=                     # make chat ONCE="one question" (otherwise an interactive chat)
TF             ?= terraform
TF_APPROVE     ?=                     # set to -auto-approve for non-interactive applies
VENV           ?= .venv
PY             ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
ECR_DIR        := infra/ecr
PLATFORM_DIR   := infra/platform
TAG_FILE       := $(PLATFORM_DIR)/.image_tag
RUNTIME_ON     := $(filter true,$(ENABLE_RUNTIME))
STACK_VARS     := -var region=$(AWS_REGION) -var name=$(NAME) -var enable_runtime=$(ENABLE_RUNTIME) -var harness_memory=$(HARNESS_MEMORY) $(if $(MODEL_ID),-var model_id=$(MODEL_ID),)

# `make deploy` uses the last pushed tag unless IMAGE_TAG is given on the command line.
ifeq ($(origin IMAGE_TAG), command line)
DEPLOY_TAG := $(IMAGE_TAG)
else
DEPLOY_TAG := $(shell cat $(TAG_FILE) 2>/dev/null)
endif
IMAGE_VAR := $(if $(RUNTIME_ON),-var image_tag=$(DEPLOY_TAG),)

OUTPUT = $$($(TF) -chdir=$(PLATFORM_DIR) output -raw $(1) 2>/dev/null)

.PHONY: help bootstrap login images deploy outputs chat smoke venv test validate destroy clean

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

bootstrap:  ## 1. create the agent's ECR repository (Runtime flavor only, once per account)
ifeq ($(RUNTIME_ON),)
	@echo "ENABLE_RUNTIME=false: no ECR repository needed, skipping"
else
	$(TF) -chdir=$(ECR_DIR) init -input=false
	$(TF) -chdir=$(ECR_DIR) apply -input=false $(TF_APPROVE) -var region=$(AWS_REGION) -var name=$(NAME)
endif

login:  ## docker login to the account's ECR registry
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $$($(TF) -chdir=$(ECR_DIR) output -raw registry)

images: login  ## 2. build and push the agent image for linux/arm64 (AgentCore requires ARM64) under IMAGE_TAG
	@echo "IMAGE_TAG=$(IMAGE_TAG)"
	@set -e; REGISTRY=$$($(TF) -chdir=$(ECR_DIR) output -raw registry); \
	docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
	  -t $$REGISTRY/$(NAME)/agent:$(IMAGE_TAG) --push services/agent; \
	docker buildx imagetools inspect $$REGISTRY/$(NAME)/agent:$(IMAGE_TAG) | grep -E 'MediaType|Platform' | head -3; \
	echo $(IMAGE_TAG) > $(TAG_FILE)

deploy:  ## 3. apply the platform stack (gateway + web search, harness, runtime + endpoint, IAM)
ifneq ($(RUNTIME_ON),)
	@test -n "$(DEPLOY_TAG)" || { echo 'no image tag: run make images first, pass IMAGE_TAG=<tag>, or deploy with ENABLE_RUNTIME=false'; exit 1; }
endif
	$(TF) -chdir=$(PLATFORM_DIR) init -input=false
	$(TF) -chdir=$(PLATFORM_DIR) apply -input=false $(TF_APPROVE) $(STACK_VARS) $(IMAGE_VAR)
	@$(if $(RUNTIME_ON),echo $(DEPLOY_TAG) > $(TAG_FILE),true)
	@$(MAKE) --no-print-directory outputs

outputs:  ## print the stack outputs (harness ARN, runtime ARN, gateway URL)
	@$(TF) -chdir=$(PLATFORM_DIR) output

chat:  ## talk to one flavor: make chat FLAVOR=harness|runtime [ONCE="question"]
	$(PY) scripts/chat.py --flavor $(FLAVOR) --region $(AWS_REGION) \
	  --arn $(call OUTPUT,$(if $(filter runtime,$(FLAVOR)),runtime_arn,harness_arn)) $(if $(ONCE),--once "$(ONCE)",)

smoke:  ## 4. ask both flavors one question that needs a web search; check the tool ran and a source URL came back
	$(PY) scripts/smoke.py --region $(AWS_REGION) --harness-arn $(call OUTPUT,harness_arn) \
	  $(if $(RUNTIME_ON),--runtime-arn $(call OUTPUT,runtime_arn),)

venv:  ## create a local virtualenv with the agent's dependencies and the test/client tools
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt -r services/agent/requirements.txt

test:  ## run the agent's unit tests (offline, no AWS calls)
	cd services/agent && ../../$(VENV)/bin/python -m pytest -q

validate:  ## terraform fmt -check + validate for both stacks (no credentials needed)
	@for d in $(ECR_DIR) $(PLATFORM_DIR); do \
	  $(TF) -chdir=$$d fmt -check -recursive && \
	  $(TF) -chdir=$$d init -backend=false -input=false >/dev/null && \
	  $(TF) -chdir=$$d validate || exit 1; \
	done

destroy:  ## tear everything down (platform, ECR, then the log groups AgentCore creates on its own for both flavors)
	@set -e; RT=$(call OUTPUT,runtime_id); HRT=$(call OUTPUT,harness_runtime_id); \
	$(TF) -chdir=$(PLATFORM_DIR) destroy -input=false $(TF_APPROVE) $(STACK_VARS) $(IMAGE_VAR); \
	if [ -d $(ECR_DIR)/.terraform ]; then $(TF) -chdir=$(ECR_DIR) destroy -input=false $(TF_APPROVE) -var region=$(AWS_REGION) -var name=$(NAME); fi; \
	for ID in $$RT $$HRT; do \
	  for LOG in $$(aws logs describe-log-groups --region $(AWS_REGION) --log-group-name-prefix /aws/bedrock-agentcore/runtimes/$$ID --query 'logGroups[].logGroupName' --output text 2>/dev/null); do \
	    aws logs delete-log-group --region $(AWS_REGION) --log-group-name "$$LOG" && echo "deleted $$LOG" || true; \
	  done; \
	done; rm -f $(TAG_FILE)

clean:  ## remove local build leftovers (virtualenv, pytest caches)
	rm -rf $(VENV) services/agent/.pytest_cache services/agent/app/__pycache__ services/agent/tests/__pycache__ scripts/__pycache__

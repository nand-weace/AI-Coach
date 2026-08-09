# ── Config ────────────────────────────────────────────────────────────────────
AWS_ACCOUNT_ID  ?= 197916995395
AWS_REGION      ?= ap-south-1
ECR_REPO        ?= nexa
IMAGE_NAME      ?= ai-coach

# Auto-increment version: reads VERSION file, bumps patch (e.g. 1.2.3 → 1.2.4)
VERSION_FILE    := VERSION
CURRENT_VERSION := $(shell cat $(VERSION_FILE) 2>/dev/null || echo "0.0.0")
MAJOR           := $(word 1,$(subst ., ,$(CURRENT_VERSION)))
MINOR           := $(word 2,$(subst ., ,$(CURRENT_VERSION)))
PATCH           := $(word 3,$(subst ., ,$(CURRENT_VERSION)))
NEW_PATCH       := $(shell expr $(PATCH) + 1)
NEW_VERSION     := $(MAJOR).$(MINOR).$(NEW_PATCH)

ECR_REGISTRY    := $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
FULL_IMAGE      := $(ECR_REGISTRY)/$(ECR_REPO)

# ── ECS config ────────────────────────────────────────────────────────────────
ECS_CLUSTER     ?= Nexa-ecs
ECS_SERVICE     ?= nexa-ecs-task-service
ECS_TASK_FAMILY ?= nexa-ecs-task
# Container inside the task definition to re-point at the new image.
# Leave empty to auto-match every container already using $(FULL_IMAGE).
ECS_CONTAINER   ?=
# Image tag to deploy. Defaults to the version `make push` is about to publish.
IMAGE_TAG       ?= $(NEW_VERSION)

# ── Targets ───────────────────────────────────────────────────────────────────
.PHONY: build tag push login deploy run version help \
        ecs-register ecs-update ecs-deploy ecs-status ecs-rollback

help:
	@echo "Targets:"
	@echo "  make build         Build the Docker image"
	@echo "  make push          Build, tag with new version, login to ECR, and push"
	@echo "  make run           Run the app locally with .env"
	@echo "  make deploy        push + restart the running container"
	@echo ""
	@echo "ECS:"
	@echo "  make ecs-deploy    push → register new task revision → update service → wait"
	@echo "  make ecs-register  Register a new revision of $(ECS_TASK_FAMILY) (IMAGE_TAG=x.y.z)"
	@echo "  make ecs-update    Point $(ECS_SERVICE) at the latest task revision"
	@echo "  make ecs-status    Show service + current task definition state"
	@echo "  make ecs-rollback  Move the service back one task definition revision"
	@echo ""
	@echo "Overrides (e.g. make push AWS_REGION=eu-west-1):"
	@echo "  AWS_ACCOUNT_ID  AWS_REGION  ECR_REPO  IMAGE_NAME"
	@echo "  ECS_CLUSTER  ECS_SERVICE  ECS_TASK_FAMILY  ECS_CONTAINER  IMAGE_TAG"

## Bump VERSION file and print the new version
version:
	@echo $(NEW_VERSION) > $(VERSION_FILE)
	@echo "Version bumped: $(CURRENT_VERSION) → $(NEW_VERSION)"

## Build the image, tagged :latest and :<version>
build: version
	docker build -t $(IMAGE_NAME):$(NEW_VERSION) -t $(IMAGE_NAME):latest .

## Authenticate Docker to ECR
login:
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

## Tag local image for ECR
tag:
	docker tag $(IMAGE_NAME):$(NEW_VERSION) $(FULL_IMAGE):$(NEW_VERSION)
	docker tag $(IMAGE_NAME):latest         $(FULL_IMAGE):latest

## Build → tag → login → push
push: build login tag
	docker push $(FULL_IMAGE):$(NEW_VERSION)
	docker push $(FULL_IMAGE):latest
	@echo "Pushed $(FULL_IMAGE):$(NEW_VERSION)"

## Run locally using .env for secrets
run:
	docker run --rm -p 5000:5000 --env-file .env $(IMAGE_NAME):latest

## push + stop old container + start new one (simple single-host deploy)
deploy: push
	-docker stop ai-coach-app 2>/dev/null
	-docker rm   ai-coach-app 2>/dev/null
	docker run -d --name ai-coach-app \
	  -p 5000:5000 \
	  --env-file .env \
	  --restart unless-stopped \
	  $(FULL_IMAGE):$(NEW_VERSION)
	@echo "Deployed $(FULL_IMAGE):$(NEW_VERSION)"

# ── ECS deployment ────────────────────────────────────────────────────────────

## Register a new revision of the task definition pointing at $(FULL_IMAGE):$(IMAGE_TAG)
ecs-register:
	@set -euo pipefail; \
	command -v jq >/dev/null || { echo "jq is required (brew install jq)"; exit 1; }; \
	echo "Checking $(ECR_REPO):$(IMAGE_TAG) exists in ECR..."; \
	aws ecr describe-images --region $(AWS_REGION) \
	  --repository-name $(ECR_REPO) --image-ids imageTag=$(IMAGE_TAG) \
	  >/dev/null || { echo "Image tag $(IMAGE_TAG) not found in ECR repo $(ECR_REPO)"; exit 1; }; \
	echo "Fetching current task definition $(ECS_TASK_FAMILY)..."; \
	aws ecs describe-task-definition --region $(AWS_REGION) \
	  --task-definition $(ECS_TASK_FAMILY) --query 'taskDefinition' --output json > /tmp/nexa-td.json; \
	NEW_IMAGE="$(FULL_IMAGE):$(IMAGE_TAG)"; \
	jq --arg img "$$NEW_IMAGE" --arg repo "$(FULL_IMAGE)" --arg name "$(ECS_CONTAINER)" ' \
	  del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, \
	      .registeredAt, .registeredBy, .deregisteredAt) \
	  | .containerDefinitions |= map( \
	      if ($$name != "" and .name == $$name) \
	         or ($$name == "" and (.image | startswith($$repo))) \
	      then .image = $$img else . end) \
	  ' /tmp/nexa-td.json > /tmp/nexa-td-new.json; \
	CHANGED=$$(jq --arg img "$$NEW_IMAGE" '[.containerDefinitions[] | select(.image == $$img)] | length' /tmp/nexa-td-new.json); \
	if [ "$$CHANGED" -eq 0 ]; then \
	  echo "No container matched. Containers in $(ECS_TASK_FAMILY):"; \
	  jq -r '.containerDefinitions[] | "  \(.name)  ->  \(.image)"' /tmp/nexa-td.json; \
	  echo "Re-run with ECS_CONTAINER=<name>"; exit 1; \
	fi; \
	echo "Registering new revision with image $$NEW_IMAGE ..."; \
	REV_ARN=$$(aws ecs register-task-definition --region $(AWS_REGION) \
	  --cli-input-json file:///tmp/nexa-td-new.json \
	  --query 'taskDefinition.taskDefinitionArn' --output text); \
	rm -f /tmp/nexa-td.json /tmp/nexa-td-new.json; \
	echo "Registered: $$REV_ARN"

## Point the service at the latest ACTIVE revision of the task definition and wait for stability
ecs-update:
	@set -euo pipefail; \
	REV_ARN=$$(aws ecs describe-task-definition --region $(AWS_REGION) \
	  --task-definition $(ECS_TASK_FAMILY) \
	  --query 'taskDefinition.taskDefinitionArn' --output text); \
	echo "Updating service $(ECS_SERVICE) on cluster $(ECS_CLUSTER) to $$REV_ARN ..."; \
	aws ecs update-service --region $(AWS_REGION) \
	  --cluster $(ECS_CLUSTER) --service $(ECS_SERVICE) \
	  --task-definition "$$REV_ARN" --force-new-deployment \
	  --query 'service.{service:serviceName,taskDefinition:taskDefinition,desired:desiredCount}' \
	  --output table; \
	echo "Waiting for the service to become stable (this can take a few minutes)..."; \
	aws ecs wait services-stable --region $(AWS_REGION) \
	  --cluster $(ECS_CLUSTER) --services $(ECS_SERVICE); \
	echo "Service is stable on $$REV_ARN"

## Full ECS release: build+push image → new task revision → roll the service
ecs-deploy: push ecs-register ecs-update
	@echo "ECS deploy complete: $(FULL_IMAGE):$(IMAGE_TAG)"

## Show current service and task definition state
ecs-status:
	@aws ecs describe-services --region $(AWS_REGION) \
	  --cluster $(ECS_CLUSTER) --services $(ECS_SERVICE) \
	  --query 'services[0].{service:serviceName,status:status,taskDefinition:taskDefinition,desired:desiredCount,running:runningCount,pending:pendingCount}' \
	  --output table
	@aws ecs describe-task-definition --region $(AWS_REGION) \
	  --task-definition $(ECS_TASK_FAMILY) \
	  --query 'taskDefinition.{family:family,revision:revision,images:containerDefinitions[].image}' \
	  --output json

## Roll the service back to the previous ACTIVE task definition revision
ecs-rollback:
	@set -euo pipefail; \
	PREV=$$(aws ecs list-task-definitions --region $(AWS_REGION) \
	  --family-prefix $(ECS_TASK_FAMILY) --status ACTIVE --sort DESC \
	  --query 'taskDefinitionArns[1]' --output text); \
	[ "$$PREV" != "None" ] || { echo "No previous revision to roll back to"; exit 1; }; \
	echo "Rolling $(ECS_SERVICE) back to $$PREV ..."; \
	aws ecs update-service --region $(AWS_REGION) \
	  --cluster $(ECS_CLUSTER) --service $(ECS_SERVICE) \
	  --task-definition "$$PREV" --query 'service.taskDefinition' --output text; \
	aws ecs wait services-stable --region $(AWS_REGION) \
	  --cluster $(ECS_CLUSTER) --services $(ECS_SERVICE); \
	echo "Rolled back to $$PREV"

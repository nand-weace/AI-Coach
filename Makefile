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

# ── Targets ───────────────────────────────────────────────────────────────────
.PHONY: build tag push login deploy run version help

help:
	@echo "Targets:"
	@echo "  make build    Build the Docker image"
	@echo "  make push     Build, tag with new version, login to ECR, and push"
	@echo "  make run      Run the app locally with .env"
	@echo "  make deploy   push + restart the running container"
	@echo ""
	@echo "Overrides (e.g. make push AWS_REGION=eu-west-1):"
	@echo "  AWS_ACCOUNT_ID  AWS_REGION  ECR_REPO  IMAGE_NAME"

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

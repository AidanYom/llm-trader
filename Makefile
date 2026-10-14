# Every target is one docker compose command; nothing runs on the host's Python (HANDOFF §13).
# On Windows, run make from Git Bash or WSL2. README.md lists each target's raw command.

.PHONY: build up down shell psql lint fmt test lock migrate revision offline dry-run submit report smoke \
	image migrate-prod report-prod tf aws secret committed push plan deploy invoke logs

# Git Bash rewrites arguments that look like Unix paths, such as /dev/null; Docker needs them as written.
export MSYS_NO_PATHCONV := 1

# The Lambda image's tag: the commit it's built from (HANDOFF §16).
TAG := $(shell git rev-parse --short HEAD)

build:
	docker compose build

up:
	docker compose up -d --wait db

down:
	docker compose down

shell:
	docker compose run --rm app bash

psql:
	docker compose exec db psql -U trader -d trader

lint:
	docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check . && mypy"

fmt:
	docker compose run --rm --no-deps app sh -c "ruff check --fix . ; ruff format ."

test:
	docker compose run --rm app pytest

lock:
	docker compose run --rm --no-deps app uv lock

migrate:
	docker compose run --rm app alembic upgrade head

revision:
	$(if $(m),,$(error usage: make revision m="describe the change"))
	docker compose run --rm app alembic revision --autogenerate -m "$(m)"

# These pass ARGS to the command, for example: make offline ARGS=--show-briefing
offline:
	docker compose run --rm app trader run --mode offline $(ARGS)

dry-run:
	docker compose run --rm app trader run --mode dry-run $(ARGS)

submit:
	docker compose run --rm app trader run --mode submit $(ARGS)

report:
	docker compose run --rm app trader report $(ARGS)

smoke:
	docker compose run --rm --no-deps app trader smoke $(ARGS)

# ---- Production (HANDOFF §16) ----

# Build the Lambda image, then check it without the network: it must import the handler and load config/.
# Lambda refuses the image index Docker's default provenance attestation creates, hence --provenance=false.
image:
	docker build --platform linux/amd64 --provenance=false --sbom=false --target lambda -t llm-trader:$(TAG) .
	docker run --rm -i --network none --entrypoint python llm-trader:$(TAG) - < docker/lambda_check.py

# Against Neon, whose URL the prod service reads from SSM with your AWS credentials.
migrate-prod:
	docker compose run --rm prod alembic upgrade head

report-prod:
	docker compose run --rm prod trader report $(ARGS)

# Terraform and the AWS CLI, in their pinned containers, for example make tf ARGS=init.
tf:
	docker compose run --rm terraform $(ARGS)

aws:
	docker compose run --rm aws $(ARGS)

# Store a secret in SSM as /llm-trader/<name>. It prompts for the value without echoing it, so the value never
# appears on screen, on a command line or in shell history. Usage: make secret name=ANTHROPIC_API_KEY
secret:
	$(if $(name),,$(error usage: make secret name=ANTHROPIC_API_KEY))
	docker compose run --rm --entrypoint bash aws -c 'read -rsp "Value for /llm-trader/$$0 (hidden): " value && echo && aws ssm put-parameter --name "/llm-trader/$$0" --type SecureString --overwrite --value "$$value" --query Version --output text' $(name)

# The image is tagged with the commit it's built from, so every change must be committed first.
committed:
	@test -z "$$(git status --porcelain)" || { echo "make: commit your changes first; the image is tagged with the commit" >&2; exit 1; }

# Push the image to ECR, unless that commit's image is already there: tags are immutable.
push: committed image
	repo=$$(docker compose run --rm -T terraform output -raw ecr_repository_url) && \
	if docker compose run --rm -T aws ecr describe-images --repository-name llm-trader --image-ids imageTag=$(TAG) >/dev/null 2>&1; then \
		echo "$$repo:$(TAG) is already in ECR"; \
	else \
		docker compose run --rm -T aws ecr get-login-password | docker login --username AWS --password-stdin "$${repo%%/*}" && \
		docker tag llm-trader:$(TAG) "$$repo:$(TAG)" && \
		docker push "$$repo:$(TAG)"; \
	fi

# What make deploy would change, without building or pushing anything.
plan:
	docker compose run --rm terraform plan -var image_tag=$(TAG)

# Push the image, then apply the Terraform with it. Terraform shows its plan and asks before changing a thing.
deploy: push
	docker compose run --rm terraform apply -var image_tag=$(TAG)

# Invoke the function now, asynchronously as the schedule does: make invoke payload='{"mode":"dry_run"}'.
# A dry_run or submit run calls Claude, which costs money. Watch it with make logs.
invoke:
	$(if $(payload),,$(error usage: make invoke payload='{"mode":"dry_run"}'))
	docker compose run --rm -T aws lambda invoke --function-name llm-trader --invocation-type Event --cli-binary-format raw-in-base64-out --payload '$(payload)' /dev/null

# Follow the function's logs from ten minutes back, or as ARGS says, for example make logs ARGS="--since 1d".
logs:
	docker compose run --rm aws logs tail /aws/lambda/llm-trader --follow --format short $(or $(ARGS),--since 10m)

# Every target is one docker compose command; nothing runs on the host's Python (HANDOFF §13).
# On Windows, run make from Git Bash or WSL2. README.md lists each target's raw command.

.PHONY: build up down shell psql lint fmt test lock migrate revision offline dry-run submit report smoke \
	image migrate-prod report-prod

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

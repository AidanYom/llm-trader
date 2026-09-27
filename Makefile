# Every target is one docker compose command; nothing runs on the host's Python (HANDOFF §13).
# On Windows, run make from Git Bash or WSL2. README.md lists each target's raw command.

.PHONY: build up down shell psql lint fmt test lock migrate revision offline dry-run submit report smoke

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

offline:
	docker compose run --rm app trader run --mode offline

dry-run:
	docker compose run --rm app trader run --mode dry-run

submit:
	docker compose run --rm app trader run --mode submit

report:
	docker compose run --rm app trader report

smoke:
	docker compose run --rm app trader smoke

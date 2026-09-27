# llm-trader

A daily trading agent for one small Alpaca account, paper trading first. Every trading day before the open, Claude reads a briefing, researches with read-only tools and proposes trades. A deterministic risk engine approves, trims or rejects each proposal, approved orders go to Alpaca, and Postgres records everything.

- **Specification:** [docs/HANDOFF.md](docs/HANDOFF.md)
- **Working agreement and milestone status:** [CLAUDE.md](CLAUDE.md)

## Prerequisites

- Docker Desktop, with Docker Compose 2.24 or later
- GNU make. On Windows, run it from Git Bash or WSL2.

Nothing else goes on the host. Python, uv (the package manager), Ruff (the linter and formatter) and pytest all run in the dev container.

## Setup

```bash
make build   # build the dev image
make test    # start Postgres and run the tests
```

API keys are only needed for `make smoke`, `make dry-run` and `make submit`. For those, copy `.env.example` to `.env` (gitignored) and fill in the keys. Keep `ALPACA_PAPER=true`.

## Make targets

Each target is a single `docker compose` command. Targets for later milestones fail until that milestone lands.

| Target | What it does | Raw command | Works from |
|---|---|---|---|
| `make build` | Build the dev image | `docker compose build` | M0 |
| `make up` | Start Postgres and wait until it's healthy | `docker compose up -d --wait db` | M0 |
| `make down` | Stop the containers (the database volume is kept) | `docker compose down` | M0 |
| `make shell` | Bash in the app container | `docker compose run --rm app bash` | M0 |
| `make psql` | psql into the dev database (run `make up` first) | `docker compose exec db psql -U trader -d trader` | M0 |
| `make lint` | Ruff lint and format checks, as in CI | `docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check ."` | M0 |
| `make fmt` | Apply Ruff's automatic fixes, then format | `docker compose run --rm --no-deps app sh -c "ruff check --fix . ; ruff format ."` | M0 |
| `make test` | pytest; integration tests use the `trader_test` database | `docker compose run --rm app pytest` | M0 |
| `make lock` | Update `uv.lock` after editing dependencies | `docker compose run --rm --no-deps app uv lock` | M0 |
| `make migrate` | Apply migrations to the dev database | `docker compose run --rm app alembic upgrade head` | M2 |
| `make revision m="add x"` | Autogenerate a migration, then review it by hand | `docker compose run --rm app alembic revision --autogenerate -m "add x"` | M2 |
| `make offline` | Full run with the fake broker and a scripted model | `docker compose run --rm app trader run --mode offline` | M3 |
| `make report` | Weekly markdown report into `reports/` | `docker compose run --rm app trader report` | M3 |
| `make smoke` | Read-only Alpaca check (needs keys) | `docker compose run --rm app trader smoke` | M4 |
| `make dry-run` | Real Alpaca and Claude, no orders (needs keys) | `docker compose run --rm app trader run --mode dry-run` | M4 |
| `make submit` | Real paper orders (needs keys) | `docker compose run --rm app trader run --mode submit` | M4 |

M5 adds `image`, `push`, `deploy` and `migrate-prod`.

## Dependencies

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. To add one, run `make shell` and then `uv add <package>`, or edit `pyproject.toml` and run `make lock`. Then run `make build`. CI fails if `uv.lock` is out of date. Dependencies beyond those listed in HANDOFF §12 need Aidan's approval.

## Configuration

`config/` arrives in M1. Each setting is documented here when it's added.

## CI

GitHub Actions runs two jobs on every push and on pull requests to `main`. Both must pass before a merge.

- `test` installs from the lock, runs Ruff's lint and format checks, then runs pytest against a Postgres 16 service.
- `image` builds the Lambda image without pushing it.

## Windows notes

- `.gitattributes` keeps every file LF. CRLF line endings break shell scripts inside the containers.
- The interactive targets (`make shell`, `make psql`) need a real terminal, such as Windows Terminal or WSL2. In the classic Git Bash window (mintty), Docker reports "the input device is not a TTY"; prefixing the command with `winpty` works around it.
- Git Bash rewrites arguments that look like absolute Unix paths, so `/app` becomes `C:/Program Files/Git/app`. The make targets avoid this. When you type such a docker command yourself, prefix it with `MSYS_NO_PATHCONV=1`.

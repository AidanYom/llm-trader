# llm-trader

A daily trading agent for one small Alpaca account, paper trading first. Every trading day before the open, Claude reads a briefing, researches with read-only tools and proposes trades. A deterministic risk engine approves, trims or rejects each proposal, approved orders go to Alpaca, and Postgres records everything.

- **Specification:** [docs/HANDOFF.md](docs/HANDOFF.md)
- **Working agreement and milestone status:** [CLAUDE.md](CLAUDE.md)

## Prerequisites

- Docker Desktop, with Docker Compose 2.24 or later
- GNU make. On Windows, run it from Git Bash or WSL2.

Nothing else goes on the host. Python, uv (the package manager), Ruff (the linter and formatter), mypy (the type checker) and pytest all run in the dev container.

## Setup

```bash
make build     # build the dev image
make test      # start Postgres and run the tests
make migrate   # create the tables in the dev database
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
| `make lint` | Ruff lint and format checks, then mypy type checks, as in CI | `docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check . && mypy"` | M0 |
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

Settings live in `config/`. `src/trader/settings.py` reads them once at startup, and the app refuses to start if a file has an unknown, missing or unusable key. Each setting is documented here when it's added. Paths are relative to the working directory: `/app` in the dev container, `/var/task` in the Lambda image.

### `config/policy.yaml`: risk limits and switches

The values are Aidan's; the file holds the current ones, and its comments explain them. Percentages are of account equity unless the table says otherwise.

| Setting | What it does | Read by |
|---|---|---|
| `trading_enabled` | Kill switch. When `false`, runs still research, evaluate and record, but send no orders. | run (M3) |
| `allow_live_money` | Must be `true` before the app will run against a non-paper Alpaca account. | run (M3) |
| `max_position_pct` | The most one symbol may hold, existing holding included. Larger buys are trimmed to fit. | risk engine |
| `max_open_positions` | The most positions open at once. Exits approved in the same run free their slots. | risk engine |
| `max_new_positions_per_week` | New symbols opened per week, Monday to Sunday. Adding to a holding doesn't count. | risk engine |
| `min_cash_buffer_pct` | Cash that buys never spend. Proceeds from sales in the same run never fund buys. | risk engine |
| `min_price` | The lowest last close, in dollars, a buy may have. | risk engine |
| `min_avg_dollar_volume` | The lowest 20-session average of close × volume, in dollars, a buy may have. | risk engine |
| `max_pct_of_adv` | The largest buy, as a % of that average. Larger buys are trimmed. | risk engine |
| `entry_limit_buffer_pct` | Buys are limit orders at the last close plus this %. | risk engine |
| `stop.required` | Must be `true`: every buy carries a protective stop. | settings |
| `stop.min_pct`, `stop.max_pct` | The allowed stop distance below the last close, in %. | risk engine |
| `drawdown_freeze_pct` | When equity is this far below its peak, buys are rejected. Sells still go through. | risk engine |
| `drawdown_peak_since` | A date: only equity from then on counts toward the peak. `null` uses all history. Set it at go-live or after a paper reset. | risk-context query |
| `blocked_symbols` | Tickers that can never be bought. Quote any that YAML would read as true, false or null, such as `'ON'`. | risk engine |

### `config/strategy.yaml`: the model, research budget and ETF universe

| Setting | What it does | Read by |
|---|---|---|
| `model` | The Claude model the agent calls. | agent (M3) |
| `max_tokens` | The most output tokens per model call. | agent (M3) |
| `max_turns` | The most model calls in one run, the nudge's included. | agent (M3) |
| `max_tool_calls` | The most research tool calls (`get_price_history`, `get_news`) in one run. | agent (M3) |
| `benchmark` | The index ETF the briefing measures relative strength against. | briefing |
| `sector_etfs`, `industry_etfs` | The ETFs in the briefing's strength table. An ETF can't be in both. | briefing |
| `baseline_basket` | The ETFs whose equal-weight buy-and-hold is the weekly report's baseline. | report (M3) |
| `news_lookback_hours` | How far back the briefing's news goes. | run (M3) |
| `max_news_items` | The most market headlines in the briefing. | briefing |
| `price` | Claude's prices in USD per million tokens, for each run's cost. | agent (M3) |
| `system_frame`, `strategy_prompt` | The two prompt files. | settings |

### Prompts

The system prompt is `config/system_frame.md`, a `---` separator, then `config/strategy.md` with its HTML comments removed. Its version, the first 10 hex characters of its SHA-256, is recorded with every run, and the `prompt_versions` table keeps each version's full text. Any edit to either file starts a new version, which is how results are attributed to prompts. `strategy.md` is Aidan's.

## Database

Postgres 16 records every run: its briefing, tool calls, proposals, verdicts, orders and cost (HANDOFF §10).

- `src/trader/db/tables.py` defines the schema with SQLAlchemy Core: table definitions plus a query builder, with no ORM.
- `src/trader/db/repo.py` holds every query, as explicit functions.
- Alembic manages the migrations in `migrations/versions/`, the way Flyway would. `make migrate` brings the dev database up to date.
- The app refuses to run against a database at any revision other than `SCHEMA_HEAD` in `tables.py`.

To change the schema:

1. Edit `src/trader/db/tables.py`.
2. Run `make migrate`, so the dev database is at head, then `make revision m="describe the change"`. Alembic drafts a migration by comparing `tables.py` with the dev database, and Ruff formats it.
3. Read the draft and fix it by hand. Autogenerate doesn't compare check constraints or partial-index conditions, so it misses changes to them. Write a working `downgrade`.
4. Set `SCHEMA_HEAD` in `tables.py` to the new revision. A unit test fails until it matches.
5. Run `make migrate && make test`.

`make test` wipes the `trader_test` database and migrates it once per run, and empties its tables before each integration test. The tests refuse any database whose name doesn't end in `_test`. One test checks that the migrations build exactly what `tables.py` describes, including the parts Alembic can't compare.

## CI

GitHub Actions runs two jobs on every push and on pull requests to `main`. Both must pass before a merge.

- `test` installs from the lock and runs Ruff's lint and format checks and mypy. Then, against a Postgres 16 service, it applies the migrations, runs `alembic check` (which fails if `tables.py` and the migrations disagree), and runs pytest.
- `image` builds the Lambda image without pushing it.

## Windows notes

- `.gitattributes` keeps every file LF. CRLF line endings break shell scripts inside the containers.
- The interactive targets (`make shell`, `make psql`) need a real terminal, such as Windows Terminal or WSL2. In the classic Git Bash window (mintty), Docker reports "the input device is not a TTY"; prefixing the command with `winpty` works around it.
- Git Bash rewrites arguments that look like absolute Unix paths, so `/app` becomes `C:/Program Files/Git/app`. The make targets avoid this. When you type such a docker command yourself, prefix it with `MSYS_NO_PATHCONV=1`.

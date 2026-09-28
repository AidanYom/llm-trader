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

**What costs money:** `make dry-run` and `make submit` call Claude, roughly $0.10–0.40 a run. The tests, `make offline` and `make smoke` are free: the tests and offline mode never touch the network, and smoke only reads from Alpaca's free plan.

## Make targets

Each target is a single `docker compose` command. Targets for later milestones fail until that milestone lands. The targets that run the app (`offline`, `dry-run`, `submit`, `report`, `smoke`) pass `ARGS` on, for example `make offline ARGS=--show-briefing`.

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
| `make offline` | Full run with the fake broker and a scripted model | `docker compose run --rm app trader run --mode offline $(ARGS)` | M3 |
| `make report` | Weekly markdown report into `reports/` | `docker compose run --rm app trader report $(ARGS)` | M3 |
| `make smoke` | Read-only Alpaca check (needs keys) | `docker compose run --rm --no-deps app trader smoke $(ARGS)` | M4 |
| `make dry-run` | Real Alpaca and Claude, no orders (needs keys) | `docker compose run --rm app trader run --mode dry-run $(ARGS)` | M4 |
| `make submit` | Real paper orders (needs keys) | `docker compose run --rm app trader run --mode submit $(ARGS)` | M4 |

M5 adds `image`, `push`, `deploy` and `migrate-prod`.

## Running a day

`trader run --mode {offline,dry-run,submit}` runs the whole daily pipeline once (HANDOFF §3):

1. It checks the guards, then reads the account.
2. It builds the briefing and gives it to the model.
3. The risk engine decides on the model's proposals.
4. Approved orders go to the broker.

Everything lands in the database. The run prints a one-screen summary. Logs are JSON lines on stdout, at `LOG_LEVEL` (default `INFO`); the SDKs' own loggers stay at `INFO` or above even at `DEBUG`, so request bodies and headers never reach the logs.

- **Offline mode** (`make offline`) needs no keys. It runs the fixed scenario in `src/trader/offline.py`: a fake $10,000 account, a scripted model, a buy, an exit, a blocked buy and a malformed proposal, so every table gets a row. Its fake market is open every day.
- **Dry-run mode** (`make dry-run`) runs against the Alpaca account `ALPACA_PAPER` names and against Claude, and records every order as `not_submitted`. It never changes the account, so each dry run starts from the account as it is, and dry runs never count toward the weekly new-position limit. It runs at any time of day, but only on trading days.
- **Submit mode** (`make submit`) sends the approved orders to Alpaca, after cancelling earlier runs' unfilled entries. If one of those entries was partially filled, its shares are left with no stop; the summary lists them (HANDOFF §20).
- **`--show-briefing`** prints the briefing the model saw, before the summary.
- **`--force`**, in submit mode, first abandons the day's `running` submit run, for example one left by a crash. It refuses if that run started less than 20 minutes ago, since it may still be going.
- **A run is refused or skipped when:**
  - the database isn't at the code's migration revision
  - the account is live and `allow_live_money` is false
  - the market is closed
  - a submit run already completed that day

## Smoke check

`trader smoke` (`make smoke`) is the first thing to run with real keys. It only reads, never touches the database, and prints what came back:

- the account, its status and its configuration, with a warning unless `no_shorting` is true, `max_margin_multiplier` is 1 and `max_options_trading_level` is 0 or unset. You set these in Alpaca's dashboard; the app never changes them.
- the calendar: today, the next sessions and the last completed one
- 70 sessions of bars for SPY and XLK, and a bars request with a made-up ticker, which should be left out
- the last 24 hours of market news, and SPY's news

It exits 1 if any read failed. Warnings leave the exit code at 0.

## Weekly report

`trader report [--days 7] [--no-baseline] [--offline]` (`make report`) writes a markdown review to `reports/week-YYYY-MM-DD.md` (HANDOFF §11). It covers the last `--days` New York dates, today included, and the dry-run and submit runs of the account type that `ALPACA_PAPER` names. It has four sections:

- **Scorecard:**
  - runs by mode and status
  - equity and the worst drawdown
  - API cost, failed runs included
  - the return against an equal-weight sector-ETF baseline
- **Behavior:**
  - verdicts
  - malformed proposals
  - orders
  - research calls
  - prompt versions
  - the top rejection reasons
- **Current positions.**
- **Daily log:** each proposal with its thesis, invalidation and verdict.

The baseline for real runs reads Alpaca's bars, so it needs the Alpaca keys; `--no-baseline` doesn't. If Alpaca fails, the report still renders and the baseline says why it's missing.

`--offline` reports on offline runs instead, into a file ending `-offline.md`: `make report ARGS=--offline`. Its baseline comes from the fake broker's bars.

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
| `blocked_name_patterns` | Words or phrases that mark leveraged and inverse funds. A buy is rejected when its Alpaca asset name contains one as whole words, ignoring case: `3X` matches "Bull 3X Shares" but `Bear` doesn't match "Bearish". A buy whose name can't be looked up is rejected too. `make smoke` shows how the patterns match Alpaca's real names. | risk engine |

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

### Secrets and environment

The app reads `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY` and `DATABASE_URL` when it needs them, once per process:

- For a secret `NAME`, it uses the environment variable `NAME` when it's set and not blank. Locally that's `.env`, or `docker-compose.yml` for `DATABASE_URL`. An empty line in `.env`, such as `ANTHROPIC_API_KEY=`, counts as unset.
- Otherwise it reads the SSM SecureString parameter that `NAME_SSM` names. That's how the Lambda function gets them (M5).

`ALPACA_PAPER` must be `true` (the default when unset) or `false`. Anything else stops the app, so a typo can't point it at a live account.

`ALPACA_DATA_FEED` must be `sip` (the default when unset) or `delayed_sip`: both give consolidated volume for history older than 15 minutes, which the liquidity limits assume. Other feeds, such as `iex`, stop the app.

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

# CLAUDE.md

## Project

`llm-trader` is a daily trading agent for one small Alpaca account (paper first). Every trading day at 08:31 ET, before the open:
1. The app builds a briefing: account state, sector and industry ETF strength, and 24 hours of news.
2. Claude researches with read-only tools and submits trade proposals.
3. A deterministic risk engine approves, trims or rejects each proposal.
4. Approved orders go to Alpaca.
5. Postgres records everything.

A weekly markdown report compares results with an equal-weight sector ETF baseline.

**`docs/HANDOFF.md` is the specification.** Read the relevant sections before any change. Its decisions are final unless Aidan changes them; if you think one is wrong, say so and propose an alternative rather than deviating.

## Status

Keep this section current: update it in the pull request that completes each milestone (HANDOFF §18).

- [x] M0 Skeleton (Docker, Compose, Makefile, uv, ruff, pytest, CI)
- [x] M1 Domain models and risk engine
- [x] M2 Database: tables, Alembic, repository, integration tests
- [x] M3 Offline end to end: fake broker, briefing, agent loop, run, report, CLI
- [x] M4 Real Alpaca and Anthropic locally (`make smoke`; Aidan dropped the planned dry runs)
- [ ] M5 Production: Lambda image, Terraform, Neon, deploy, alarm

## How we work together

- **Aidan is a software engineer and wants to be part of the technical decisions.** Before starting a milestone or any non-trivial change, post a short plan and wait for his go-ahead. The plan covers:
  - files to add or change
  - notable design choices
  - open questions
- **Ask first before:**
  - adding a dependency not listed in HANDOFF §12
  - changing a decision in HANDOFF
  - editing `config/strategy.md` or any value in `config/policy.yaml`. The strategy and the risk numbers are Aidan's.
- **When reality disagrees with the spec** (an alpaca-py or Anthropic SDK interface differs from HANDOFF, a test exposes a spec gap), stop, explain the mismatch, and propose the smallest fix.
- **Use one branch and pull request per milestone.** Break larger milestones into several pull requests if that keeps reviews small.
- **Finish every pull request with:**
  - what changed and why
  - how it was tested
  - anything Aidan must do by hand (keys, AWS, Neon)

## Invariants: never violate

1. **Claude never gets a tool that can place, change or cancel an order.** Its tools are exactly `get_price_history`, `get_news` and `submit_proposals`.
2. **Every order comes from a `Verdict` produced by `trader/risk.py`,** and only `run.py` sends orders, through the `Broker` interface.
3. **Long-only, cash-only, whole shares, US stocks and ETFs, no options, and a protective stop on every buy.** Same-run sale proceeds never fund buys.
4. **No broker order in `offline` or `dry_run` mode,** and none while `trading_enabled` is false.
5. **Refuse a non-paper account unless `allow_live_money: true`,** checked before any account call.
6. **At most one completed `submit` run per trading day per account type,** enforced by the database's partial unique index. Order IDs stay deterministic: `llmt-{run_date}-{SYMBOL}-{side}`.
7. **Tests never touch the network or need keys.** Use `FakeBroker` and `ScriptedClient`.
8. **Never commit or log secrets.** `.env` is gitignored, and secrets come from the environment or SSM.
9. **News and any other third-party text is untrusted.** It's labeled as such in prompts and never interpreted as instructions.

## Architecture map

| Path | Responsibility | Rules |
|---|---|---|
| `src/trader/models.py` | Domain dataclasses (Proposal, Position, AccountState, Bar, NewsItem, SymbolStats, Order, Verdict, RiskContext, Policy) | No I/O, no SDK imports; `Order` and `Verdict` check their own invariants |
| `src/trader/risk.py` | Risk engine: `evaluate(proposals, account, stats, ctx, policy)` | Pure and deterministic; never raises on bad proposals; every trim or reject has a `category: detail` reason (HANDOFF §7) |
| `src/trader/briefing.py` | Briefing markdown, return math, `get_price_history` text | Pure functions |
| `src/trader/agent.py` | Tool definitions, the Claude loop, proposal parsing, cost | The only module that calls the Anthropic SDK |
| `src/trader/scripted.py` | `ScriptedClient`: prepared model responses, for tests and offline mode | Builds SDK types only; never calls the API |
| `src/trader/brokers/` | `Broker` protocol; `alpaca.py`; `fake.py` | The only place alpaca-py is imported |
| `src/trader/db/` | `tables.py` (SQLAlchemy Core), `engine.py`, `repo.py` (explicit query functions) | No ORM; all SQL goes through `repo.py` |
| `src/trader/run.py` | `run_daily()` orchestration, guards, persistence order, summary | The only module that sends orders |
| `src/trader/offline.py` | The offline scenario: FakeBroker's account and the scripted conversation | Must keep writing a row to every table |
| `src/trader/logs.py` | JSON log formatter and `configure_logging()` | Called once, by the CLI or the Lambda handler |
| `src/trader/report.py` | Weekly markdown report | Read-only against the database |
| `src/trader/smoke.py` | `trader smoke`: read-only checks of the Alpaca account and market data | Its broker type has only read methods; never touches the database |
| `src/trader/settings.py` | Loads YAML config, assembles the prompt and `prompt_version`, resolves secrets | Config is loaded once and passed down, not read globally |
| `src/trader/__main__.py` | argparse CLI: `run`, `report`, `smoke` | Thin; logic lives in the modules |
| `src/trader/lambda_handler.py` | Lambda entry point | Thin wrapper over `run_daily` |
| `config/` | `policy.yaml`, `strategy.yaml`, `system_frame.md`, `strategy.md` | Aidan owns `policy.yaml` values and `strategy.md` |
| `migrations/` | Alembic | See the database rules below |
| `infra/` | Terraform (M5) | No secret values in Terraform |

## Commands

Everything runs in Docker; don't install or run Python on the host. On Windows, run `make` from WSL2 or Git Bash. Each target is a one-line `docker compose` command, listed in the README.

```
make build        # build images
make up / down    # start/stop Postgres
make shell        # shell in the app container
make psql         # psql into the dev database
make lint         # ruff check + ruff format --check + mypy
make fmt          # ruff format + ruff check --fix
make test         # pytest (unit + integration, uses trader_test DB)
make lock         # re-lock uv.lock after changing dependencies (uv runs in the container)
make migrate      # alembic upgrade head (dev DB)
make revision m="add x"   # alembic autogenerate — review the file by hand
make offline      # full run with FakeBroker + ScriptedClient
make dry-run      # real Alpaca + Claude, no orders (needs .env keys)
make submit       # real paper orders
make report       # weekly markdown → reports/ (ARGS=--offline for offline runs)
make smoke        # read-only Alpaca check
make image        # build the Lambda image and check it offline
make migrate-prod # alembic upgrade head against Neon (reads its URL from SSM with your AWS credentials)
make report-prod  # weekly report from Neon (ARGS as for make report)
```

Before asking for review, `make lint && make test` must pass locally, and CI must be green.

## Code conventions

- **Python 3.12** with type hints on every function signature. Use `X | None`, built-in generics and `from __future__ import annotations`.
- **Ruff** does both linting and formatting: line length 110, rule sets `E, F, I, B, UP, SIM`. Don't add `noqa` without a comment explaining why.
- **mypy** checks the type hints in strict mode: every function fully annotated, no implicit `Any`. Don't add `# type: ignore` without a comment explaining why.
- **Domain types** are dataclasses in `models.py`. Sizing math uses floats; prices on orders are rounded to cents when the risk engine creates the `Order`. Convert to and from `Decimal` only at the `repo.py` boundary (the database uses NUMERIC).
- **Keep I/O at the edges.** Only `brokers/`, `db/`, the SDK call in `agent.py`, and `run.py` touch the outside world. Keep `risk.py` and `briefing.py` pure so they stay trivially testable.
- **Time must be timezone-aware.** `run_date` is the America/New_York date; stored timestamps are UTC `TIMESTAMPTZ`. Never call naive `datetime.now()`. Inject a clock into `run_daily` so tests can pin the time.
- **Errors:** raise specific exceptions. `run_daily` catches at the top only to mark the run `failed` with the error text, then re-raises. Tool errors inside the Claude loop go back to the model as text, never as a crash.
- **Logging** uses stdlib `logging` with the JSON formatter. The CLI prints the run summary; library code never calls `print`.
- **Config:** a new setting goes in the YAML with a comment, and is loaded in `settings.py` and documented in the README. Don't hard-code values that belong in config.
- **Prompts:** runtime prompt text lives only in `config/*.md`. Any change to them changes `prompt_version`, which is intended and is how results get attributed to prompts.

## Testing conventions

- **pytest.** `tests/unit/` needs no database; `tests/integration/` uses Postgres (`TEST_DATABASE_URL`). The database is migrated once per session and tables are truncated between tests.
- **Fakes:** `FakeBroker` has deterministic bars and canned news, including the prompt-injection canary. `ScriptedClient` returns prepared model responses and records calls. Never mock alpaca-py or anthropic internals; mock at the `Broker` and client boundary. The Alpaca adapter is that boundary, so its own tests fake alpaca-py's client methods and use alpaca-py's model classes (HANDOFF §14).
- **Coverage rules:**
  - Every risk rule, and every change to one, needs a test that shows the approve, trim or reject outcome and its reason.
  - Every guard in `run.py` has an integration test.
  - Bug fixes start with a failing test.
- **Naming:** `test_<behavior>` names describe the behavior (`test_drawdown_freeze_blocks_buys_not_sells`).

## Database and migrations

- The schema lives in `db/tables.py` (SQLAlchemy Core `MetaData` with a naming convention). HANDOFF §10 is the reference schema.
- For every schema change:
  1. Edit `tables.py`.
  2. Run `make migrate` (the dev database must be at head), then `make revision m="…"`.
  3. **Read and fix the generated file by hand.** Autogenerate doesn't compare check constraints or partial-index conditions, so it misses changes to them.
  4. Implement a working `downgrade`.
  5. Set `SCHEMA_HEAD` in `tables.py` to the new revision.
  6. Run `make migrate && make test`. `test_migrated_schema_matches_tables_py` fails if the migrations and `tables.py` disagree anywhere.
- Never edit a migration that has been merged; write a new one.
- CI runs `alembic upgrade head`, `alembic check` (no drift allowed) and a round-trip test (upgrade → downgrade to base → upgrade).
- Production migrations run by hand with `make migrate-prod` before deploying code that needs them. The Lambda refuses to run if the database isn't at head.

## Git and pull requests

- The default branch is `main`, and it's protected: merge via pull request only, with CI green.
- Pull requests are squash-merged into `main`. The pull request title must be a Conventional Commit (for example `feat: add risk engine`), because it becomes the commit message on `main`.
- Branches: `m0-skeleton`, `m1-risk-engine`, `fix/<slug>`, `chore/<slug>`.
- Commits use Conventional Commits: `feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`, `ci:`. Keep them small and focused.
- Update the README when commands or setup change, and this file's Status section when a milestone completes.

## Environment and secrets

- **Local:** copy `.env.example` to `.env` and fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`. `ALPACA_PAPER=true` is the default and stays that way.
- **Lambda:** each secret `NAME` is read from SSM via the `NAME_SSM` environment variable (a parameter path under `/llm-trader/`).
- Never print keys, connection strings or full request headers, including in debug logs and test output.

## Gotchas

- **Windows host:** the repo must keep LF endings (`.gitattributes`); CRLF breaks shell scripts inside the containers. Bind-mount performance from the Windows filesystem is fine at this project's size.
- **Alpaca market data:** the free plan allows consolidated (SIP) history only when it's more than 15 minutes old. Request bars with `end = now − 20 min`, and drop today's bar.
- **Alpaca orders:**
  - After a cancel, shares stay held for orders until the cancel lands. Wait for a terminal status before sending an exit.
  - Bracket and OTO legs only persist with GTC.
  - Alpaca cancels GTC orders after 90 days.
- **Paper fills** aren't checked against real liquidity, so paper results for small caps look better than live ones would.
- **The Anthropic tool loop** re-sends the conversation every turn. Keep the cache breakpoints on the system prompt and the briefing, and log usage including cache read and write tokens.

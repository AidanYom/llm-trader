"""Configuration loading (HANDOFF §4 and §12). Config is read once at startup and passed down, never read
globally.

`load_config()` reads `config/policy.yaml`, `config/strategy.yaml` and the two prompt files, and assembles the
system prompt and its version. The paths are relative to the working directory (HANDOFF §13): /app in the dev
container, /var/task in the Lambda image.

`Secrets` resolves the API keys and the database URL from the environment, or from SSM in Lambda.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from trader.models import Policy, Prices, StopPolicy, Strategy, finite_float, normalize_symbol

POLICY_FILE = Path("config/policy.yaml")
STRATEGY_FILE = Path("config/strategy.yaml")

# HANDOFF §4: the system prompt is system_frame.md, this separator, then strategy.md.
PROMPT_SEPARATOR = "\n\n---\n\n"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_LINES = re.compile(r"\n(?:[ \t]*\n){2,}")  # two or more blank lines in a row


class ConfigError(ValueError):
    """A config file is missing, malformed, or holds a value the app can't use."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Everything a run reads from `config/`, loaded once at startup."""

    policy: Policy
    strategy: Strategy
    system_prompt: str
    prompt_version: str  # the first 10 hex characters of the system prompt's SHA-256


def load_config(root: Path) -> Config:
    """Read the config files under `root`, the directory their paths are relative to."""
    strategy = load_strategy(root / STRATEGY_FILE)
    system_prompt = assemble_system_prompt(
        _read_text(root / strategy.system_frame), _read_text(root / strategy.strategy_prompt)
    )
    return Config(
        policy=load_policy(root / POLICY_FILE),
        strategy=strategy,
        system_prompt=system_prompt,
        prompt_version=prompt_version(system_prompt),
    )


def assemble_system_prompt(system_frame: str, strategy: str) -> str:
    """HANDOFF §4: the frame, a separator, then the strategy without its HTML comments.

    In each part, runs of blank lines collapse to one, and leading and trailing whitespace is trimmed. The
    files are read with universal newlines, so a CRLF checkout gives the same prompt.
    """
    parts = (system_frame, _HTML_COMMENT.sub("", strategy))
    return PROMPT_SEPARATOR.join(_BLANK_LINES.sub("\n\n", part).strip() for part in parts)


def prompt_version(system_prompt: str) -> str:
    """The first 10 hex characters of the prompt's SHA-256 (HANDOFF §4)."""
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:10]


# ---- The environment --------------------------------------------------------------------------------------


class SecretError(RuntimeError):
    """A secret is set nowhere, or SSM couldn't return it. Messages name the secret, never a value."""


class SsmClient(Protocol):
    """The one call Secrets makes on boto3's SSM client."""

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, Any]: ...


class Secrets:
    """Secrets such as ANTHROPIC_API_KEY and DATABASE_URL, each resolved once per process (HANDOFF §13).

    For a secret NAME: the environment variable NAME if it's set and not blank; otherwise the SSM SecureString
    parameter that NAME_SSM names, decrypted. A blank NAME counts as unset, because an empty line in .env,
    such as `ANTHROPIC_API_KEY=`, still sets the variable.
    """

    def __init__(self, environ: Mapping[str, str], ssm: Callable[[], SsmClient] | None = None) -> None:
        self._environ = environ
        self._make_ssm = ssm or _boto3_ssm
        self._ssm: SsmClient | None = None
        self._resolved: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._resolved:
            self._resolved[name] = self._environ.get(name, "").strip() or self._from_ssm(name)
        return self._resolved[name]

    def _from_ssm(self, name: str) -> str:
        parameter = self._environ.get(f"{name}_SSM", "").strip()
        if not parameter:
            raise SecretError(
                f"{name} is not set: set it in .env, or set {name}_SSM to an SSM parameter name"
            )
        if self._ssm is None:
            self._ssm = self._make_ssm()
        try:
            value = self._ssm.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
        except Exception as exc:  # whatever failed, the secret is unavailable
            # boto3's error messages name the parameter and the error code, never the value.
            problem = f"{type(exc).__name__}: {exc}"
            raise SecretError(f"{name}: can't read SSM parameter {parameter}: {problem}") from exc
        if not isinstance(value, str) or not value.strip():
            raise SecretError(f"{name}: SSM parameter {parameter} is empty")
        return value.strip()


def _boto3_ssm() -> SsmClient:
    # Imported here, so local runs and tests never load boto3. It ships without type hints, and this one call
    # doesn't justify adding boto3-stubs as a dependency.
    import boto3  # type: ignore[import-untyped]

    client: SsmClient = boto3.client("ssm")
    return client


def alpaca_paper(environ: Mapping[str, str]) -> bool:
    """ALPACA_PAPER: true when unset (HANDOFF §8). Only true and false are accepted: a typo can't go live."""
    value = environ.get("ALPACA_PAPER", "").strip().lower()
    if value in ("", "true"):
        return True
    if value == "false":
        return False
    raise ConfigError(f"ALPACA_PAPER must be true or false, got {value!r}")


ALPACA_DATA_FEEDS = ("sip", "delayed_sip")


def alpaca_data_feed(environ: Mapping[str, str]) -> str:
    """ALPACA_DATA_FEED: sip when unset, or delayed_sip (HANDOFF §8).

    Both give consolidated volume for history older than 15 minutes. Other feeds are refused: iex sees only a
    few percent of the market's volume, so the liquidity floor would reject nearly every buy.
    """
    value = environ.get("ALPACA_DATA_FEED", "").strip().lower()
    if value == "":
        return "sip"
    if value not in ALPACA_DATA_FEEDS:
        raise ConfigError(
            f"ALPACA_DATA_FEED must be sip or delayed_sip, got {value!r}: "
            "the liquidity limits assume consolidated volume"
        )
    return value


# A number's allowed range: the check, and how an error message describes it.
_Range = tuple[Callable[[float], bool], str]
_ABOVE_0: _Range = (lambda v: v > 0, "above 0")
_AT_LEAST_0: _Range = (lambda v: v >= 0, "of at least 0")
_PERCENT: _Range = (lambda v: 0 < v <= 100, "above 0 and at most 100")
_PERCENT_OR_0: _Range = (lambda v: 0 <= v <= 100, "from 0 to 100")
_STOP_PERCENT: _Range = (lambda v: 0 < v < 100, "above 0 and below 100")


def load_policy(path: Path) -> Policy:
    """Read `policy.yaml` into a Policy.

    Raises ConfigError naming the key when one is unknown, missing or unusable. The checks only catch
    values the code can't work with; whether a limit is wise is Aidan's call.
    """
    top = _Section(_read_yaml(path), path, "", _field_names(Policy))
    stop = top.section("stop", _field_names(StopPolicy))
    if not stop.flag("required"):
        # CLAUDE.md invariant 3; the risk engine requires a stop on every buy regardless.
        raise stop.error("required", "must be true: every buy carries a protective stop")
    stop_policy = StopPolicy(
        required=True,
        min_pct=stop.number("min_pct", _STOP_PERCENT),
        max_pct=stop.number("max_pct", _STOP_PERCENT),
    )
    if stop_policy.min_pct > stop_policy.max_pct:
        raise stop.error("min_pct", f"must not be above stop.max_pct ({stop_policy.max_pct:g})")
    return Policy(
        trading_enabled=top.flag("trading_enabled"),
        allow_live_money=top.flag("allow_live_money"),
        max_position_pct=top.number("max_position_pct", _PERCENT),
        max_open_positions=top.count("max_open_positions"),
        max_new_positions_per_week=top.count("max_new_positions_per_week"),
        min_cash_buffer_pct=top.number("min_cash_buffer_pct", _PERCENT_OR_0),
        min_price=top.number("min_price", _ABOVE_0),
        min_avg_dollar_volume=top.number("min_avg_dollar_volume", _AT_LEAST_0),
        max_pct_of_adv=top.number("max_pct_of_adv", _PERCENT),
        entry_limit_buffer_pct=top.number("entry_limit_buffer_pct", _PERCENT_OR_0),
        stop=stop_policy,
        drawdown_freeze_pct=top.number("drawdown_freeze_pct", _PERCENT),
        drawdown_peak_since=top.optional_date("drawdown_peak_since"),
        blocked_symbols=top.symbols("blocked_symbols"),
    )


def load_strategy(path: Path) -> Strategy:
    """Read `strategy.yaml` into a Strategy, raising ConfigError naming the key when one is unusable."""
    top = _Section(_read_yaml(path), path, "", _field_names(Strategy))
    price = top.section("price", _field_names(Prices))
    sector_etfs = top.tickers("sector_etfs")
    industry_etfs = top.tickers("industry_etfs")
    if both := [symbol for symbol in industry_etfs if symbol in sector_etfs]:
        # The briefing's ETF table gives each ETF one type.
        raise top.error("industry_etfs", f"{', '.join(both)} also listed in sector_etfs")
    return Strategy(
        model=top.text("model"),
        max_tokens=top.count("max_tokens", minimum=1),
        max_turns=top.count("max_turns", minimum=1),
        max_tool_calls=top.count("max_tool_calls"),
        benchmark=top.ticker("benchmark"),
        sector_etfs=sector_etfs,
        industry_etfs=industry_etfs,
        baseline_basket=top.tickers("baseline_basket"),
        news_lookback_hours=top.number("news_lookback_hours", _ABOVE_0),
        max_news_items=top.count("max_news_items"),
        price=Prices(
            input=price.number("input", _AT_LEAST_0),
            output=price.number("output", _AT_LEAST_0),
            cache_write=price.number("cache_write", _AT_LEAST_0),
            cache_read=price.number("cache_read", _AT_LEAST_0),
        ),
        system_frame=top.text("system_frame"),
        strategy_prompt=top.text("strategy_prompt"),
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")  # universal newlines: CRLF reads as LF
    except OSError as exc:
        raise ConfigError(f"{path}: can't read the file: {exc.strerror or exc}") from exc


def _read_yaml(path: Path) -> object:
    text = _read_text(path)
    try:
        return yaml.safe_load(text)
    # PyYAML raises a plain ValueError for an impossible date such as 2026-13-01.
    except (yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc


def _field_names(cls: type[Any]) -> frozenset[str]:
    return frozenset(field.name for field in dataclasses.fields(cls))


class _Section:
    """One YAML mapping. It must have exactly the expected keys; the accessors check each value."""

    def __init__(self, data: object, path: Path, prefix: str, keys: frozenset[str]) -> None:
        self._path = path
        self._prefix = prefix  # "" at the top level, "stop." inside stop
        if not isinstance(data, dict):
            where = f"{prefix.rstrip('.')}: " if prefix else ""
            raise ConfigError(f"{path}: {where}expected a mapping of keys to values, got {data!r}")
        self._data: dict[Any, Any] = data
        unknown = sorted(str(key) for key in data if key not in keys)
        missing = sorted(key for key in keys if key not in data)
        problems = [
            f"{label} key{'s' if len(names) > 1 else ''} {', '.join(prefix + name for name in names)}"
            for label, names in (("unknown", unknown), ("missing", missing))
            if names
        ]
        if problems:
            raise ConfigError(f"{path}: {'; '.join(problems)}")

    def error(self, key: str, problem: str) -> ConfigError:
        return ConfigError(f"{self._path}: {self._prefix}{key}: {problem}")

    def section(self, key: str, keys: frozenset[str]) -> _Section:
        return _Section(self._data[key], self._path, f"{self._prefix}{key}.", keys)

    def flag(self, key: str) -> bool:
        value = self._data[key]
        if not isinstance(value, bool):
            raise self.error(key, f"must be true or false, got {value!r}")
        return value

    def count(self, key: str, *, minimum: int = 0) -> int:
        value = self._data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self.error(key, f"must be a whole number >= {minimum}, got {value!r}")
        return value

    def number(self, key: str, allowed: _Range) -> float:
        value = self._data[key]
        number = finite_float(value)
        check, description = allowed
        if number is None or not check(number):
            raise self.error(key, f"must be a number {description}, got {value!r}")
        return number

    def text(self, key: str) -> str:
        value = self._data[key]
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, f"must be a non-empty string, got {value!r}")
        return value

    def optional_date(self, key: str) -> date | None:
        value = self._data[key]
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise self.error(key, f"must be null or a date such as 2026-10-01, got {value!r}")

    def ticker(self, key: str) -> str:
        return self._ticker(key, self._data[key])

    def tickers(self, key: str) -> tuple[str, ...]:
        """An ordered list of tickers, each listed once."""
        symbols: list[str] = []
        for item in self._list(key):
            symbol = self._ticker(key, item)
            if symbol in symbols:
                raise self.error(key, f"{symbol} is listed twice")
            symbols.append(symbol)
        return tuple(symbols)

    def symbols(self, key: str) -> frozenset[str]:
        return frozenset(self._ticker(key, item) for item in self._list(key))

    def _list(self, key: str) -> list[object]:
        value = self._data[key]
        if not isinstance(value, list):
            raise self.error(key, f"must be a list of tickers, got {value!r}")
        return value

    def _ticker(self, key: str, item: object) -> str:
        symbol = normalize_symbol(item)
        if symbol is None:
            hint = ""
            if item is None or isinstance(item, bool):
                hint = (
                    "; YAML reads unquoted words such as ON, YES, NO and NULL as true, false or null, "
                    "so quote them"
                )
            raise self.error(key, f"{item!r} is not a ticker{hint}")
        return symbol

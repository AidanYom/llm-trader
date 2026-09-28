from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from trader.settings import (
    PROMPT_SEPARATOR,
    ConfigError,
    SecretError,
    Secrets,
    SsmClient,
    alpaca_data_feed,
    alpaca_paper,
    assemble_system_prompt,
    load_config,
    load_policy,
    load_strategy,
    prompt_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_POLICY = REPO_ROOT / "config" / "policy.yaml"
SHIPPED_STRATEGY = REPO_ROOT / "config" / "strategy.yaml"


def edit(path: Path, key: str, line: str) -> Path:
    """Replace the line for `key` in the file with `line`, keeping its indentation."""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*){re.escape(key)}:.*$", re.MULTILINE)
    assert pattern.search(text), f"no {key} line in {path.name}"
    path.write_text(pattern.sub(lambda match: match.group(1) + line, text, count=1), encoding="utf-8")
    return path


def policy_file(tmp_path: Path, key: str, line: str) -> Path:
    """The shipped policy.yaml with one line replaced.

    Tests change keys, not values, so they keep passing when Aidan tunes the numbers.
    """
    return edit(Path(shutil.copy(SHIPPED_POLICY, tmp_path)), key, line)


def strategy_file(tmp_path: Path, key: str, line: str) -> Path:
    return edit(Path(shutil.copy(SHIPPED_STRATEGY, tmp_path)), key, line)


def config_root(tmp_path: Path) -> Path:
    """A copy of the shipped config/ directory under tmp_path, for tests that change it."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    return tmp_path


# ---- policy.yaml ------------------------------------------------------------------------------------------


def test_shipped_policy_loads() -> None:
    policy = load_policy(SHIPPED_POLICY)

    assert policy.stop.required
    assert policy.stop.min_pct <= policy.stop.max_pct
    assert policy.blocked_symbols
    assert all(symbol == symbol.upper() for symbol in policy.blocked_symbols)


def test_misspelt_key_is_named_as_unknown_and_missing(tmp_path: Path) -> None:
    path = policy_file(tmp_path, "max_open_positions", "max_open_positons: 6")

    with pytest.raises(ConfigError, match="unknown key max_open_positons; missing key max_open_positions"):
        load_policy(path)


def test_missing_nested_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing key stop\.min_pct"):
        load_policy(policy_file(tmp_path, "min_pct", ""))


def test_stop_not_required_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"stop\.required: must be true"):
        load_policy(policy_file(tmp_path, "required", "required: false"))


@pytest.mark.parametrize(
    ("key", "line", "message"),
    [
        ("trading_enabled", 'trading_enabled: "yes"', "trading_enabled: must be true or false, got 'yes'"),
        ("max_open_positions", "max_open_positions: 6.5", "max_open_positions: must be a whole number >= 0"),
        ("min_price", "min_price: -1", "min_price: must be a number above 0, got -1"),
        ("max_position_pct", "max_position_pct: 150", "must be a number above 0 and at most 100, got 150"),
        ("max_pct", "max_pct: .nan", "stop.max_pct: must be a number above 0 and below 100, got nan"),
        ("min_pct", "min_pct: 99.9", "stop.min_pct: must not be above stop.max_pct"),
        ("drawdown_peak_since", "drawdown_peak_since: soon", "drawdown_peak_since: must be null or a date"),
        ("drawdown_peak_since", "drawdown_peak_since: 2026-10-01 09:30:00", "must be null or a date"),
        ("drawdown_peak_since", "drawdown_peak_since: 2026-13-01", "not valid YAML"),
        ("blocked_symbols", "blocked_symbols: TQQQ", "blocked_symbols: must be a list of tickers"),
        ("blocked_symbols", "blocked_symbols: [TQQQ, BRK B]", "blocked_symbols: 'BRK B' is not a ticker"),
        ("blocked_symbols", "blocked_symbols: [TQQQ, ON]", "True is not a ticker; YAML reads unquoted"),
    ],
)
def test_unusable_value_is_named(tmp_path: Path, key: str, line: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_policy(policy_file(tmp_path, key, line))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("drawdown_peak_since: 2026-10-01", date(2026, 10, 1)),
        ('drawdown_peak_since: "2026-10-01"', date(2026, 10, 1)),
        ("drawdown_peak_since: null", None),
    ],
)
def test_peak_since_is_a_date_or_null(tmp_path: Path, line: str, expected: date | None) -> None:
    policy = load_policy(policy_file(tmp_path, "drawdown_peak_since", line))

    assert policy.drawdown_peak_since == expected


def test_blocked_symbols_are_normalized(tmp_path: Path) -> None:
    policy = load_policy(policy_file(tmp_path, "blocked_symbols", "blocked_symbols: [tqqq, ' sqqq', 'ON']"))

    assert policy.blocked_symbols == frozenset({"TQQQ", "SQQQ", "ON"})


def name_patterns(tmp_path: Path, patterns: str) -> Path:
    """The shipped policy.yaml with blocked_name_patterns, its last setting and a block list, replaced."""
    text = SHIPPED_POLICY.read_text(encoding="utf-8")
    head = text[: text.index("\nblocked_name_patterns:")]
    path = tmp_path / "policy.yaml"
    path.write_text(f"{head}\nblocked_name_patterns: {patterns}\n", encoding="utf-8")
    return path


def test_shipped_name_patterns_load() -> None:
    patterns = load_policy(SHIPPED_POLICY).blocked_name_patterns

    assert "3X" in patterns
    assert "ProShares Ultra" in patterns


def test_name_patterns_keep_their_order_with_whitespace_collapsed(tmp_path: Path) -> None:
    policy = load_policy(name_patterns(tmp_path, "['3X', ' ProShares   Ultra ', Bear]"))

    assert policy.blocked_name_patterns == ("3X", "ProShares Ultra", "Bear")


def test_an_empty_list_of_name_patterns_checks_no_names(tmp_path: Path) -> None:
    assert load_policy(name_patterns(tmp_path, "[]")).blocked_name_patterns == ()


@pytest.mark.parametrize(
    ("patterns", "message"),
    [
        ("Bear", "blocked_name_patterns: must be a list of words or phrases, got 'Bear'"),
        ("[Bear, bear]", "blocked_name_patterns: bear is listed twice"),
        ("[Bear, '--']", "blocked_name_patterns: '--' is not a word or phrase"),
        ("[Bear, 3]", "blocked_name_patterns: 3 is not a word or phrase"),
        ("[Bear, NO]", "False is not a word or phrase; YAML reads unquoted"),
    ],
)
def test_unusable_name_patterns_are_named(tmp_path: Path, patterns: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_policy(name_patterns(tmp_path, patterns))


def test_unreadable_or_empty_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="can't read the file"):
        load_policy(tmp_path / "missing.yaml")

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="expected a mapping of keys to values, got None"):
        load_policy(empty)


# ---- strategy.yaml ----------------------------------------------------------------------------------------


def test_shipped_strategy_loads() -> None:
    strategy = load_strategy(SHIPPED_STRATEGY)

    assert strategy.max_tokens >= 1 and strategy.max_turns >= 1
    universe = (strategy.benchmark, *strategy.sector_etfs, *strategy.industry_etfs, *strategy.baseline_basket)
    assert all(symbol == symbol.upper() for symbol in universe)
    assert strategy.sector_etfs and strategy.baseline_basket
    assert strategy.price.output >= strategy.price.input >= strategy.price.cache_read


def test_strategy_tickers_keep_their_order(tmp_path: Path) -> None:
    strategy = load_strategy(strategy_file(tmp_path, "sector_etfs", "sector_etfs: [xlk, ' XLE', XLF]"))

    assert strategy.sector_etfs == ("XLK", "XLE", "XLF")


def test_misspelt_strategy_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key max_turn; missing key max_turns"):
        load_strategy(strategy_file(tmp_path, "max_turns", "max_turn: 15"))


@pytest.mark.parametrize(
    ("key", "line", "message"),
    [
        ("model", 'model: ""', "model: must be a non-empty string, got ''"),
        ("max_tokens", "max_tokens: 0", "max_tokens: must be a whole number >= 1, got 0"),
        ("max_turns", "max_turns: 2.5", "max_turns: must be a whole number >= 1, got 2.5"),
        ("max_tool_calls", "max_tool_calls: -1", "max_tool_calls: must be a whole number >= 0, got -1"),
        ("benchmark", "benchmark: [SPY]", "benchmark: ['SPY'] is not a ticker"),
        ("sector_etfs", "sector_etfs: [XLK, xlk]", "sector_etfs: XLK is listed twice"),
        ("industry_etfs", "industry_etfs: [SMH, XLE]", "industry_etfs: XLE also listed in sector_etfs"),
        ("baseline_basket", "baseline_basket: XLK", "baseline_basket: must be a list of tickers"),
        ("news_lookback_hours", "news_lookback_hours: 0", "news_lookback_hours: must be a number above 0"),
        ("input", "input: -2", "price.input: must be a number of at least 0, got -2"),
        ("system_frame", "system_frame: null", "system_frame: must be a non-empty string, got None"),
    ],
)
def test_unusable_strategy_value_is_named(tmp_path: Path, key: str, line: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_strategy(strategy_file(tmp_path, key, line))


# ---- The system prompt and its version --------------------------------------------------------------------


def test_system_prompt_is_the_frame_then_the_strategy_without_comments() -> None:
    frame = "You are the agent.\n\n\n\nRules:\n- one\n"
    strategy = "# Strategy\n\n<!-- Aidan's file.\n   It spans lines. -->\n  \n\n**Horizon:** days.\n"

    assert assemble_system_prompt(frame, strategy) == (
        "You are the agent.\n\nRules:\n- one\n\n---\n\n# Strategy\n\n**Horizon:** days."
    )


def test_prompt_version_is_the_start_of_the_sha256() -> None:
    assert prompt_version("abc") == "ba7816bf8f"  # SHA-256("abc") = ba7816bf8f01cfea…


def test_config_is_read_from_under_the_root() -> None:
    config = load_config(REPO_ROOT)

    frame = (REPO_ROOT / "config" / "system_frame.md").read_text(encoding="utf-8")
    strategy = (REPO_ROOT / "config" / "strategy.md").read_text(encoding="utf-8")
    assert config.system_prompt == assemble_system_prompt(frame, strategy)
    assert config.system_prompt.count(PROMPT_SEPARATOR) == 1
    assert "<!--" not in config.system_prompt
    assert config.prompt_version == prompt_version(config.system_prompt)
    assert (config.policy, config.strategy) == (load_policy(SHIPPED_POLICY), load_strategy(SHIPPED_STRATEGY))


def test_crlf_prompt_files_give_the_same_version(tmp_path: Path) -> None:
    root = config_root(tmp_path)
    for name in ("system_frame.md", "strategy.md"):
        path = root / "config" / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    assert load_config(root).prompt_version == load_config(REPO_ROOT).prompt_version


def test_editing_the_strategy_changes_the_version(tmp_path: Path) -> None:
    root = config_root(tmp_path)
    path = root / "config" / "strategy.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nMost days, do nothing.\n", encoding="utf-8")

    assert load_config(root).prompt_version != load_config(REPO_ROOT).prompt_version


def test_missing_prompt_file_is_reported(tmp_path: Path) -> None:
    root = config_root(tmp_path)
    edit(root / "config" / "strategy.yaml", "system_frame", "system_frame: config/missing.md")

    with pytest.raises(ConfigError, match=r"missing\.md: can't read the file"):
        load_config(root)


# ---- Secrets and ALPACA_PAPER -----------------------------------------------------------------------------


class FakeSsm:
    """Stands in for boto3's SSM client, recording each parameter it's asked for."""

    def __init__(self, parameters: Mapping[str, str]) -> None:
        self.parameters = parameters
        self.requests: list[tuple[str, bool]] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, Any]:
        self.requests.append((Name, WithDecryption))
        if Name not in self.parameters:
            raise LookupError(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]}}


def no_ssm() -> SsmClient:
    raise AssertionError("SSM must not be called")


def test_environment_variable_wins() -> None:
    secrets = Secrets({"ANTHROPIC_API_KEY": "sk-env", "ANTHROPIC_API_KEY_SSM": "/llm-trader/X"}, ssm=no_ssm)

    assert secrets.get("ANTHROPIC_API_KEY") == "sk-env"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_environment_variable_falls_back_to_ssm(blank: str) -> None:
    ssm = FakeSsm({"/llm-trader/ANTHROPIC_API_KEY": "sk-ssm"})
    environ = {"ANTHROPIC_API_KEY": blank, "ANTHROPIC_API_KEY_SSM": "/llm-trader/ANTHROPIC_API_KEY"}

    assert Secrets(environ, ssm=lambda: ssm).get("ANTHROPIC_API_KEY") == "sk-ssm"
    assert ssm.requests == [("/llm-trader/ANTHROPIC_API_KEY", True)]  # decrypted


def test_each_secret_is_read_from_ssm_once() -> None:
    ssm = FakeSsm({"/llm-trader/DATABASE_URL": "postgresql://db", "/llm-trader/ANTHROPIC_API_KEY": "sk"})
    clients: list[FakeSsm] = []

    def make_ssm() -> SsmClient:
        clients.append(ssm)
        return ssm

    secrets = Secrets(
        {
            "DATABASE_URL_SSM": "/llm-trader/DATABASE_URL",
            "ANTHROPIC_API_KEY_SSM": "/llm-trader/ANTHROPIC_API_KEY",
        },
        ssm=make_ssm,
    )
    for _ in range(2):
        assert secrets.get("DATABASE_URL") == "postgresql://db"
        assert secrets.get("ANTHROPIC_API_KEY") == "sk"

    assert len(ssm.requests) == 2
    assert len(clients) == 1


def test_secret_set_nowhere_names_both_variables() -> None:
    with pytest.raises(
        SecretError, match="ALPACA_API_KEY is not set: set it in .env, or set ALPACA_API_KEY_SSM"
    ):
        Secrets({"ALPACA_API_KEY": ""}, ssm=no_ssm).get("ALPACA_API_KEY")


def test_ssm_failure_names_the_parameter() -> None:
    secrets = Secrets({"DATABASE_URL_SSM": "/llm-trader/DATABASE_URL"}, ssm=lambda: FakeSsm({}))

    with pytest.raises(SecretError, match="DATABASE_URL: can't read SSM parameter /llm-trader/DATABASE_URL"):
        secrets.get("DATABASE_URL")


def test_empty_ssm_parameter_is_refused() -> None:
    secrets = Secrets({"DATABASE_URL_SSM": "/p"}, ssm=lambda: FakeSsm({"/p": " "}))

    with pytest.raises(SecretError, match="SSM parameter /p is empty"):
        secrets.get("DATABASE_URL")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("", True), ("true", True), ("TRUE ", True), ("false", False), ("False", False)],
)
def test_alpaca_paper_defaults_to_true(value: str | None, expected: bool) -> None:
    assert alpaca_paper({} if value is None else {"ALPACA_PAPER": value}) is expected


@pytest.mark.parametrize("value", ["yes", "0", "live", "fasle"])
def test_alpaca_paper_refuses_anything_but_true_or_false(value: str) -> None:
    with pytest.raises(ConfigError, match="ALPACA_PAPER must be true or false"):
        alpaca_paper({"ALPACA_PAPER": value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "sip"), ("", "sip"), ("sip", "sip"), (" SIP ", "sip"), ("delayed_sip", "delayed_sip")],
)
def test_alpaca_data_feed_defaults_to_sip(value: str | None, expected: str) -> None:
    assert alpaca_data_feed({} if value is None else {"ALPACA_DATA_FEED": value}) == expected


@pytest.mark.parametrize("value", ["iex", "otc", "boats", "overnight", "sips"])
def test_alpaca_data_feed_refuses_feeds_without_consolidated_volume(value: str) -> None:
    with pytest.raises(ConfigError, match="ALPACA_DATA_FEED must be sip or delayed_sip"):
        alpaca_data_feed({"ALPACA_DATA_FEED": value})

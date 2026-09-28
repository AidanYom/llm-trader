from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from trader import lambda_handler
from trader.brokers.alpaca import AlpacaBroker
from trader.brokers.fake import FakeBroker
from trader.lambda_handler import EventError, handler, run_options
from trader.logs import JsonFormatter
from trader.models import RunMode, RunStatus
from trader.run import RunResult
from trader.scripted import ScriptedClient
from trader.settings import SecretError, Secrets, SsmClient

NOW = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)
RUN_ID = uuid4()
DATABASE_URL = "postgresql://trader:not-real@neon.example/trader"
PARAMETERS = {
    "/llm-trader/ALPACA_API_KEY": "alpaca-key-not-real",
    "/llm-trader/ALPACA_SECRET_KEY": "alpaca-secret-not-real",
    "/llm-trader/ANTHROPIC_API_KEY": "anthropic-key-not-real",
    "/llm-trader/DATABASE_URL": DATABASE_URL,
}
# The *_SSM variables Terraform gives the function.
LAMBDA_ENVIRONMENT = {f"{name.rsplit('/', 1)[1]}_SSM": name for name in PARAMETERS}


class FakeSsm:
    """Stands in for boto3's SSM client, recording each parameter it's asked for."""

    def __init__(self, parameters: Mapping[str, str]) -> None:
        self.parameters = parameters
        self.requests: list[str] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, Any]:
        assert WithDecryption
        self.requests.append(Name)
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]}}


class FakeEngine:
    def __init__(self, url: str) -> None:
        self.url = url
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


@dataclass
class Wiring:
    """What the handler built and called, with run_daily and the database replaced."""

    ssm: FakeSsm
    result: RunResult | Exception
    runs: list[dict[str, Any]] = field(default_factory=list)
    engines: list[FakeEngine] = field(default_factory=list)
    claude_keys: list[str] = field(default_factory=list)


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> Wiring:
    """A fresh execution environment: secrets in (fake) SSM, and nothing yet read from it."""
    wired = Wiring(
        ssm=FakeSsm(PARAMETERS),
        result=RunResult(run_id=RUN_ID, status=RunStatus.COMPLETED, summary="2026-09-28 · submit (paper)"),
    )

    def make_ssm() -> SsmClient:
        return wired.ssm

    def claude(api_key: str) -> ScriptedClient:
        wired.claude_keys.append(api_key)
        return ScriptedClient([])

    def make_engine(url: str) -> FakeEngine:
        wired.engines.append(FakeEngine(url))
        return wired.engines[-1]

    def recorded_run(**kwargs: Any) -> RunResult:
        wired.runs.append(kwargs)
        if isinstance(wired.result, Exception):
            raise wired.result
        return wired.result

    monkeypatch.setattr(lambda_handler, "SECRETS", Secrets(LAMBDA_ENVIRONMENT, ssm=make_ssm))
    monkeypatch.setattr(lambda_handler, "anthropic_client", claude)
    monkeypatch.setattr(lambda_handler, "make_engine", make_engine)
    monkeypatch.setattr(lambda_handler, "run_daily", recorded_run)
    monkeypatch.delenv("RUN_MODE", raising=False)
    return wired


# ---- The event ---------------------------------------------------------------------------------------------


def test_the_event_mode_wins_over_run_mode() -> None:
    assert run_options({"mode": "submit"}, {"RUN_MODE": "dry_run"}) == (RunMode.SUBMIT, False)
    assert run_options({"mode": "dry_run"}, {"RUN_MODE": "submit"}) == (RunMode.DRY_RUN, False)


@pytest.mark.parametrize("event", [{}, None, {"mode": None}, {"mode": ""}, {"force": False}])
def test_run_mode_applies_when_the_event_names_no_mode(event: object) -> None:
    assert run_options(event, {"RUN_MODE": " submit "}) == (RunMode.SUBMIT, False)


@pytest.mark.parametrize(
    ("event", "environ", "message"),
    [
        (
            {"mode": "offline"},
            {"RUN_MODE": "submit"},
            "the event's mode must be dry_run or submit, got 'offline'",
        ),
        ({"mode": "dry-run"}, {}, "the event's mode must be dry_run or submit, got 'dry-run'"),
        ({"mode": False}, {"RUN_MODE": "submit"}, "the event's mode must be dry_run or submit, got False"),
        ({"mode": ["submit"]}, {}, "the event's mode must be dry_run or submit, got ['submit']"),
        ({}, {"RUN_MODE": "offline"}, "RUN_MODE must be dry_run or submit, got 'offline'"),
        ({}, {}, "RUN_MODE must be dry_run or submit, got ''"),
    ],
)
def test_only_dry_run_and_submit_are_accepted(event: object, environ: dict[str, str], message: str) -> None:
    with pytest.raises(EventError) as exc_info:
        run_options(event, environ)

    assert str(exc_info.value) == message


def test_force_comes_from_the_event_and_must_be_true_or_false() -> None:
    assert run_options({"mode": "submit", "force": True}, {}) == (RunMode.SUBMIT, True)
    with pytest.raises(EventError, match="^the event's force must be true or false, got 'yes'$"):
        run_options({"mode": "submit", "force": "yes"}, {})


@pytest.mark.parametrize("event", [["submit"], "submit", 1])
def test_the_event_must_be_an_object(event: object) -> None:
    with pytest.raises(EventError, match="^the event must be a JSON object, got "):
        run_options(event, {"RUN_MODE": "submit"})


def test_a_bad_mode_fails_before_anything_is_read_or_run(wiring: Wiring) -> None:
    """The alarm test in the rollout invokes {"mode": "bogus"}: it must fail without a Claude call."""
    with pytest.raises(EventError):
        handler({"mode": "bogus"}, None)

    assert (wiring.ssm.requests, wiring.claude_keys, wiring.runs) == ([], [], [])


# ---- The run -----------------------------------------------------------------------------------------------


def test_the_handler_runs_the_day_and_returns_json(wiring: Wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeBroker(now=NOW)
    monkeypatch.setattr(lambda_handler, "_alpaca", lambda secrets: broker)

    result = handler({"mode": "submit", "force": True}, None)

    (call,) = wiring.runs
    (engine,) = wiring.engines
    assert (call["mode"], call["force"]) == (RunMode.SUBMIT, True)
    assert (call["broker"], call["engine"]) == (broker, engine)
    assert call["config"].prompt_version  # config/ loaded from the working directory
    assert (engine.url, engine.disposed) == (DATABASE_URL, True)
    assert result == {"run_id": str(RUN_ID), "status": "completed", "summary": "2026-09-28 · submit (paper)"}
    assert json.loads(json.dumps(result)) == result


def test_secrets_are_read_from_ssm_once_across_warm_invocations(wiring: Wiring) -> None:
    handler({"mode": "dry_run"}, None)
    handler({"mode": "dry_run"}, None)

    assert sorted(wiring.ssm.requests) == sorted(PARAMETERS)  # each parameter once, for both invocations
    assert wiring.claude_keys == ["anthropic-key-not-real"] * 2
    assert [engine.url for engine in wiring.engines] == [DATABASE_URL] * 2
    broker = wiring.runs[0]["broker"]
    assert isinstance(broker, AlpacaBroker)
    assert broker.is_paper


def test_a_missing_secret_stops_the_run_before_it_starts(
    wiring: Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lambda_handler, "SECRETS", Secrets({}))

    with pytest.raises(SecretError, match="^ALPACA_API_KEY is not set"):
        handler({"mode": "submit"}, None)

    assert (wiring.runs, wiring.engines) == ([], [])


def test_a_failed_run_is_re_raised_after_the_engine_is_disposed(wiring: Wiring) -> None:
    """Re-raising makes the invocation fail, which the function's Errors alarm counts."""
    wiring.result = RuntimeError("Alpaca is down")

    with pytest.raises(RuntimeError, match="^Alpaca is down$"):
        handler({"mode": "submit"}, None)

    (engine,) = wiring.engines
    assert engine.disposed


def test_logs_are_json_at_log_level(wiring: Wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    handler({"mode": "dry_run"}, None)

    root = logging.getLogger()
    (log_handler,) = root.handlers
    assert isinstance(log_handler.formatter, JsonFormatter)
    assert root.level == logging.DEBUG

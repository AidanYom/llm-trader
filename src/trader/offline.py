"""The offline scenario: what `make offline` runs, against FakeBroker and ScriptedClient (HANDOFF §9 and §18).

The fake account has cash, holds XLE and SMH with their protective stop orders, and has yesterday's unfilled
IGV entry still open. Its market opens every day, so the scenario also runs at weekends. The script researches
XLE and URA, then submits:

- a buy of URA, a new position
- a sell of SMH, a full exit whose legs are cancelled first
- a buy of TQQQ, which the blocklist rejects
- one malformed proposal

So a run writes a row to every table. The script never sees the tool results: its words are canned.
"""

from __future__ import annotations

from datetime import datetime

from trader.brokers.fake import FakeBroker, Holding, OpenOrder
from trader.models import Side
from trader.scripted import ScriptedClient, reply, submit, text, tool_use

MARKET_VIEW = (
    "Energy and uranium lead the ETF table on 1-month strength versus SPY, backed by the OPEC+ cuts and new "
    "utility supply contracts. Semiconductors are rolling over on the export restrictions, so the SMH "
    "position has played out."
)


def offline_broker(now: datetime, *, paper: bool = True) -> FakeBroker:
    """A $10,000-ish fake account at `now`, whose market is open every day."""
    closes = FakeBroker(now=now)  # to set the holdings' entry prices from the fake's own history
    entry = {symbol: closes.completed_sessions(symbol)[-21].close for symbol in ("XLE", "SMH")}  # a month ago
    return FakeBroker(
        now=now,
        cash=7_000.0,
        holdings=[
            Holding(symbol="XLE", qty=4, avg_entry_price=entry["XLE"]),
            Holding(symbol="SMH", qty=5, avg_entry_price=entry["SMH"]),
        ],
        open_orders=[
            OpenOrder(broker_order_id="xle-stop", symbol="XLE", side=Side.SELL),
            OpenOrder(broker_order_id="smh-stop", symbol="SMH", side=Side.SELL),
            OpenOrder(broker_order_id="smh-take-profit", symbol="SMH", side=Side.SELL),
            OpenOrder(broker_order_id="igv-entry", symbol="IGV", side=Side.BUY),
        ],
        is_paper=paper,
        open_every_day=True,
    )


def offline_client() -> ScriptedClient:
    """Two research turns, then one submit_proposals call."""
    return ScriptedClient(
        [
            reply(
                text("Energy and uranium lead the table. Checking both before deciding."),
                tool_use("get_price_history", {"symbol": "XLE"}),
                tool_use("get_news", {"symbol": "URA"}),
            ),
            reply(tool_use("get_price_history", {"symbol": "URA", "days": 30})),
            reply(
                submit(
                    MARKET_VIEW,
                    [
                        {
                            "symbol": "URA",
                            "action": "buy",
                            "target_pct": 5,
                            "stop_pct": 8,
                            "take_profit_pct": 15,
                            "thesis": "Uranium leads on 1-month strength after long-term supply contracts.",
                            "invalidation": "URA closes below its 20-day low.",
                            "confidence": 0.6,
                        },
                        {
                            "symbol": "SMH",
                            "action": "sell",
                            "thesis": "Export restrictions ended the semiconductor thesis.",
                            "invalidation": "The restrictions are withdrawn.",
                            "confidence": 0.7,
                        },
                        {
                            "symbol": "TQQQ",
                            "action": "buy",
                            "target_pct": 5,
                            "stop_pct": 10,
                            "thesis": "Leveraged exposure to the Nasdaq rebound.",
                            "invalidation": "QQQ falls below its 20-day low.",
                            "confidence": 0.3,
                        },
                        {
                            "symbol": "XLE",
                            "action": "hold",
                            "thesis": "Keep the energy position.",
                            "invalidation": "Oil falls below its 20-day low.",
                            "confidence": 0.5,
                        },
                    ],
                )
            ),
        ]
    )

"""The running market, its configuration, and the history the UI charts.

Kept apart from the HTTP layer because it has a life of its own: the market runs
whether or not anyone is watching, it can be rebuilt with a different
configuration while the server stays up, and it accumulates a price history that
outlives any one WebSocket connection.

Rebuilding rather than mutating is deliberate. Half the point of this venue is
that a run is reproducible from its seed, and an agent population edited
mid-flight would produce a session that no seed could ever reproduce. Changing
the configuration therefore starts a new market, and the UI says so.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import numpy as np

from arena.exchange.session import SessionState
from arena.market.fees import FREE, MAKER_TAKER, FeeSchedule
from arena.market.live import HUMAN_ID, LiveMarket
from arena.research.stylized import analyse
from dashboard.build_market import build, true_values

__all__ = ["MarketConfig", "MarketRunner", "FEE_SCHEDULES"]

# Named schedules, so the browser sends a name rather than raw basis points and
# cannot invent a venue that pays out more than it takes without meaning to.
FEE_SCHEDULES: dict[str, FeeSchedule] = {
    "free": FREE,
    "maker-taker": MAKER_TAKER,
    "taker-only": FeeSchedule(taker_bps=3.0, maker_bps=0.0),
    "generous": FeeSchedule(taker_bps=1.0, maker_bps=-2.0),
}

# How many samples of each symbol's price to keep, and how many server ticks
# pass between samples. The server ticks at 20Hz; sampling every fourth tick
# gives 5Hz, so this buffer covers six minutes of wall clock rather than ninety
# seconds -- long enough for a chart to show a session rather than a moment.
HISTORY = 1_800
# Every second tick rather than every fourth. A step costs 0.12ms against a
# 50ms budget, so the old rate was leaving a chart to fill slowly for no
# reason anyone could point at.
SAMPLE_EVERY = 2


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """Everything that decides what market is running."""

    seed: int = 7
    speed: float = 1.0
    arbitrageur: bool = False
    flow_traders: int = 0
    fees: str = "maker-taker"
    price_band: float | None = 0.05
    makers: int = 3
    opening_auction: bool = True
    surface: bool = True
    mechanism: str = "book"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "speed": self.speed,
            "arbitrageur": self.arbitrageur,
            "flow_traders": self.flow_traders,
            "fees": self.fees,
            "price_band": self.price_band,
            "makers": self.makers,
            "opening_auction": self.opening_auction,
            "surface": self.surface,
            "mechanism": self.mechanism,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MarketConfig":
        """Build from untrusted JSON, clamping rather than trusting.

        Everything here arrives from a browser, so every field is bounded. An
        unbounded agent count or speed would let a page freeze the server that
        is serving it.
        """
        band = payload.get("price_band", 0.05)
        fees = str(payload.get("fees", "maker-taker"))
        return cls(
            seed=int(payload.get("seed", 7)) % (2**31),
            speed=max(0.0, min(50.0, float(payload.get("speed", 1.0)))),
            arbitrageur=bool(payload.get("arbitrageur", False)),
            flow_traders=max(0, min(24, int(payload.get("flow_traders", 0)))),
            fees=fees if fees in FEE_SCHEDULES else "free",
            price_band=(
                None if band in (None, "", "none") else max(0.001, min(5.0, float(band)))
            ),
            makers=max(1, min(8, int(payload.get("makers", 3)))),
            opening_auction=bool(payload.get("opening_auction", True)),
            surface=bool(payload.get("surface", True)),
            mechanism=(
                "scoring-rule"
                if str(payload.get("mechanism", "book")) == "scoring-rule"
                else "book"
            ),
        )


@dataclass
class Series:
    """One symbol's recent price path, for charting."""

    stamps: deque[int] = field(default_factory=lambda: deque(maxlen=HISTORY))
    mids: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY))
    trades: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY))
    signs: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY))

    def to_dict(self) -> dict[str, list[Any]]:
        return {
            "t": list(self.stamps),
            "mid": [None if np.isnan(v) else v for v in self.mids],
        }


def _role(agent) -> str:
    """What an agent does, for a reader who does not know the class names.

    Published beside the class so the page can say "market maker" while the
    diagnostics keep the exact type. It is derived by isinstance rather than
    by name, so a specialised maker is still a maker -- which is the thing two
    tests got wrong when the options maker arrived and they went looking for
    the string "MarketMaker".
    """
    from arena.agents.arbitrageur import Arbitrageur
    from arena.agents.bayesian import BayesianFundamental
    from arena.agents.flow import FlowTrader
    from arena.agents.fundamental import FundamentalTrader
    from arena.agents.market_maker import MarketMaker
    from arena.agents.noise import NoiseTrader

    for kind, label in (
        (MarketMaker, "market maker"),
        (Arbitrageur, "arbitrageur"),
        (BayesianFundamental, "informed"),
        (FundamentalTrader, "informed"),
        (FlowTrader, "order flow"),
        (NoiseTrader, "uninformed"),
    ):
        if isinstance(agent, kind):
            return label
    return "participant"


class MarketRunner:
    """Owns the live market, steps it, and records what happened."""

    def __init__(self, config: MarketConfig | None = None) -> None:
        self.config = config or MarketConfig()
        self.market: LiveMarket = self._build(self.config)
        self.history: dict[str, Series] = {}
        self.generation = 0
        self.started_at = time.time()
        self._tape_seen: dict[str, int] = {}
        self._ticks = 0
        self._reset_history()

    # -- lifecycle ---------------------------------------------------------

    def _build(self, config: MarketConfig) -> LiveMarket:
        market = build(
            seed=config.seed,
            speed=config.speed,
            arbitrageur=config.arbitrageur,
            flow_traders=config.flow_traders,
            fees=FEE_SCHEDULES[config.fees],
            price_band=config.price_band,
            makers=config.makers,
            opening_auction=config.opening_auction,
            surface=config.surface,
            mechanism=config.mechanism,
        )
        return market

    def _reset_history(self) -> None:
        self.history = {s: Series() for s in self.market.venue.registry.symbols}
        self._tape_seen = dict.fromkeys(self.market.venue.registry.symbols, 0)

    def start(self) -> None:
        self.market.start()

    def reconfigure(self, config: MarketConfig) -> dict[str, Any]:
        """Swap in a new market. The old one is discarded, not paused.

        A configuration change starts a fresh session precisely so the result
        stays reproducible from its seed.
        """
        self.config = config
        self.market = self._build(config)
        self.generation += 1
        self.started_at = time.time()
        self._reset_history()
        self.start()
        return {"ok": True, "generation": self.generation, "config": config.to_dict()}

    def set_speed(self, speed: float) -> dict[str, Any]:
        """Speed is the one setting that does not need a rebuild.

        It changes how fast simulated time tracks the wall clock, not what any
        agent decides, so the session stays the one the seed describes.
        """
        value = max(0.0, min(50.0, float(speed)))
        self.market.speed = value
        self.config = replace(self.config, speed=value)
        return {"ok": True, "speed": value}

    # -- stepping ----------------------------------------------------------

    def step(self) -> int:
        events = self.market.step()
        self._ticks += 1
        if self._ticks % SAMPLE_EVERY == 0:
            self._record()
        return events

    def _record(self) -> None:
        venue = self.market.venue
        now = int(self.market.kernel.now)
        for symbol, series in self.history.items():
            instrument = venue.registry.require(symbol)
            engine = venue.engine(symbol)
            snapshot = engine.book.snapshot(1)
            mid = snapshot.mid
            series.stamps.append(now)
            series.mids.append(
                float("nan") if mid is None else float(instrument.from_ticks(int(mid)))
            )
            tape = engine.tape
            seen = self._tape_seen.get(symbol, 0)
            for trade in tape[seen:]:
                series.trades.append(float(instrument.from_ticks(trade.price)))
                series.signs.append(1.0 if trade.aggressor_side.value == "buy" else -1.0)
            self._tape_seen[symbol] = len(tape)

    # -- reporting ---------------------------------------------------------

    def session_state(self) -> dict[str, Any]:
        venue = self.market.venue
        return {
            "generation": self.generation,
            "uptime": time.time() - self.started_at,
            "config": self.config.to_dict(),
            "fees": venue.fees.to_dict(),
            "fees_collected": str(venue.fees_collected),
            "price_band": venue.price_band,
            "halts": venue.halts[-20:],
            "sessions": {
                symbol: venue.session(symbol).value
                for symbol in venue.registry.symbols
            },
        }

    def agents(self) -> list[dict[str, Any]]:
        """Who is in the market. The population is the experiment."""
        roster = []
        for agent in self.market.agents:
            account = self.market.venue.accounts.get(agent.agent_id)
            roster.append(
                {
                    "id": str(agent.agent_id),
                    "kind": type(agent).__name__,
                    "role": _role(agent),
                    "fills": getattr(agent, "fills", 0),
                    "rejects": getattr(agent, "rejects", 0),
                    "positions": {
                        s: q for s, q in sorted(getattr(agent, "position", {}).items()) if q
                    },
                    "equity": (
                        str(account.equity(self.market.venue.marks()))
                        if account is not None
                        else None
                    ),
                }
            )
        return roster

    def diagnostics(self, symbol: str) -> dict[str, Any]:
        """Stylized-fact diagnostics for one symbol, from the live history.

        The same estimators the research harness uses, so a number seen in the
        browser is the number a paper would quote rather than a second
        implementation that might disagree.
        """
        series = self.history.get(symbol)
        if series is None:
            return {"error": f"unknown symbol {symbol}"}
        mids = np.array([v for v in series.mids if not np.isnan(v)], dtype=float)
        trades = np.asarray(series.trades, dtype=float)
        signs = np.asarray(series.signs, dtype=float)
        if mids.size < 60:
            return {
                "symbol": symbol,
                "observations": int(mids.size),
                "pending": True,
                "verdicts": [],
            }
        report = analyse(symbol, mids, trades, signs)
        return report.to_dict()

    def instruments(self) -> list[dict[str, Any]]:
        venue = self.market.venue
        settle = true_values(
            [venue.registry.require(s) for s in venue.registry.symbols]
        )
        out = []
        for symbol in venue.registry.symbols:
            instrument = venue.registry.require(symbol)
            payload = instrument.to_dict()
            payload["session"] = venue.session(symbol).value
            payload["trades"] = len(venue.engine(symbol).tape)
            # What it will actually be worth, **as a price**. Only meaningful
            # because this is a simulation, and the whole reason the market can
            # be scored rather than merely watched.
            #
            # In ticks until now, while every price on the page beside it was
            # in contract units. So the one number whose entire job is to be
            # compared against the market was in a different unit from the
            # market: SPIKE_WR_FUT marked at 4,663 and revealed a "settlement"
            # of 18,677, and the chart drew that as a target line four times
            # off the top of the series. It looked like a number rather than
            # like a bug, which is why it survived.
            ticks = settle.get(symbol)
            payload["settles_at"] = (
                None if ticks is None else float(instrument.from_ticks(round(ticks)))
            )
            out.append(payload)
        return out

    def halt(self, symbol: str) -> dict[str, Any]:
        venue = self.market.venue
        if venue.registry.get(symbol) is None:
            return {"ok": False, "error": f"unknown symbol {symbol}"}
        venue.halt(symbol, reason="manual")
        return {"ok": True, "session": venue.session(symbol).value}

    def uncross(self, symbol: str) -> dict[str, Any]:
        venue = self.market.venue
        if venue.registry.get(symbol) is None:
            return {"ok": False, "error": f"unknown symbol {symbol}"}
        if venue.session(symbol) is SessionState.CONTINUOUS:
            return {"ok": False, "error": f"{symbol} is already trading continuously"}
        result = venue.uncross(symbol)
        return {
            "ok": True,
            "session": venue.session(symbol).value,
            "auction": None if result is None else result.to_dict(),
        }

    def indicative(self, symbol: str) -> dict[str, Any] | None:
        venue = self.market.venue
        if venue.registry.get(symbol) is None:
            return None
        result = venue.indicative(symbol)
        return None if result is None else result.to_dict()

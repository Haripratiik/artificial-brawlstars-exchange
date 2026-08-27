"""Arena Markets: the exchange, served to a browser.

    python -m dashboard.server
    # then open http://127.0.0.1:8000

The browser is a *viewer of the simulator*, never a second implementation of it.
Every order a person clicks is enqueued onto the same human agent an algorithm
would be, travels the same latency link, and is checked by the same collateral
code. A UI that applied orders directly would be showing you a market you are
not actually in.

Layout of the surface:

    GET  /api/instruments      what is listed, and the terms of each contract
    GET  /api/session          phases, halts, fees, the running configuration
    GET  /api/agents           who else is in the market
    GET  /api/history/{sym}    price path, for charting
    GET  /api/diagnostics/{s}  stylized-fact report on the live series
    GET  /api/book/{sym}       a deeper ladder than the socket carries
    POST /api/config           rebuild the market with a new configuration
    POST /api/session/{sym}/halt      suspend trading
    POST /api/session/{sym}/uncross   clear the call and resume
    WS   /ws                   live snapshot at the tick rate, and order entry

Single-process and single-market by design. A second viewer sees the *same*
market, which is the useful behaviour when you want the book on one screen and
the blotter on another.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import secrets
import mimetypes
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from arena.exchange.types import AgentId
from dashboard.identity import COOKIE, display_name, sign, verify
from dashboard.state import FEE_SCHEDULES, MarketConfig, MarketRunner

# Starlette serves static files with whatever `mimetypes` reports, and on
# Windows `mimetypes` reads the registry -- where `.js` is very often mapped to
# `text/plain`. Browsers enforce the MIME type of `<script type="module">`
# strictly and refuse a module served as anything but JavaScript, so the entire
# front end silently did not run: the page rendered its static HTML, no handler
# was bound, and nothing in the server logs said why, because every request had
# answered 200.
#
# Registered here rather than relied upon, so the answer does not depend on the
# machine the server happens to start on.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# How often the kernel is advanced and a snapshot goes out. 20 Hz is past what
# an eye resolves and cheap enough that the simulation never waits on a socket.
TICK_SECONDS = 0.05

# Sessions this process has seated, by cookie session id. Not a database: the
# accounts they name live in the running market, so both go away together.
_SEATS: dict[str, AgentId] = {}


app = FastAPI(title="Arena Markets")
runner = MarketRunner()
_pump: asyncio.Task | None = None


async def _run_market() -> None:
    """Advance the kernel in step with the wall clock, forever."""
    runner.start()
    while True:
        try:
            runner.step()
        except Exception as failure:  # keep serving, and report it
            print(f"market step failed: {failure!r}")
        await asyncio.sleep(TICK_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    global _pump
    _pump = asyncio.create_task(_run_market())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _pump is not None:
        _pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _pump


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@app.get("/")
async def index(request: Request) -> FileResponse:
    """The page, and a session cookie for whoever asked for it.

    Issued on the first visit rather than behind a sign-in form, so someone who
    opens the exchange can trade immediately with their own account and rename
    themselves afterwards if they want to. The alternative -- a wall between a
    visitor and the market -- is the wrong default for a place whose capital is
    imaginary.
    """
    response = FileResponse(STATIC / "index.html")
    _ensure_session(request, response)
    return response


def _seat_for(request_or_socket: Any) -> AgentId | None:
    """The account this connection is signed in as, if any.

    Returns ``None`` for a connection with no valid cookie, and the caller
    falls back to the shared account -- which is what every test and every
    direct API user gets, unchanged.
    """
    payload = verify(request_or_socket.cookies.get(COOKIE))
    if payload is None:
        return None
    seat = _SEATS.get(str(payload.get("sid", "")))
    return seat


def _ensure_session(request: Request, response: Response) -> AgentId:
    """Resolve the cookie to an account, seating a new one if needed.

    The cookie carries a session id and a name; the *account* is looked up from
    the session id in this process. So a cookie from a previous run names a
    session this market has never heard of, and the visitor is seated afresh --
    which is right, because the accounts that cookie referred to went away with
    the market that held them.
    """
    payload = verify(request.cookies.get(COOKIE)) or {}
    sid = str(payload.get("sid", ""))
    name = display_name(payload.get("name"))

    seat = _SEATS.get(sid)
    if seat is None:
        sid = secrets.token_urlsafe(12)
        seat = runner.market.seat(name)
        _SEATS[sid] = seat
        response.set_cookie(
            COOKIE,
            sign({"sid": sid, "name": name}),
            max_age=7 * 24 * 3600,
            httponly=False,
            samesite="lax",
        )
    return seat


@app.post("/api/me")
async def api_rename(request: Request, response: Response) -> dict[str, Any]:
    """Change the name other traders see. There is nothing else to change."""
    body = await request.json()
    seat = _ensure_session(request, response)
    name = display_name(str(body.get("name", "")))
    agent = runner.market.traders.get(seat)
    if agent is not None:
        agent.display_name = name
    payload = verify(request.cookies.get(COOKIE)) or {}
    sid = str(payload.get("sid", "")) or next(
        (k for k, v in _SEATS.items() if v == seat), ""
    )
    response.set_cookie(
        COOKIE,
        sign({"sid": sid, "name": name}),
        max_age=7 * 24 * 3600,
        httponly=False,
        samesite="lax",
    )
    return {"ok": True, "id": str(seat), "name": name}


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


@app.get("/api/instruments")
async def api_instruments() -> dict[str, Any]:
    return {"instruments": runner.instruments()}


@app.get("/api/session")
async def api_session() -> dict[str, Any]:
    payload = runner.session_state()
    payload["fee_schedules"] = {
        name: schedule.to_dict() for name, schedule in FEE_SCHEDULES.items()
    }
    return payload


@app.get("/api/agents")
async def api_agents() -> dict[str, Any]:
    return {"agents": runner.agents()}


@app.get("/api/history/{symbol}")
async def api_history(symbol: str) -> JSONResponse:
    series = runner.history.get(symbol)
    if series is None:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    return JSONResponse({"symbol": symbol, **series.to_dict()})


@app.get("/api/diagnostics/{symbol}")
async def api_diagnostics(symbol: str) -> JSONResponse:
    report = runner.diagnostics(symbol)
    if "error" in report:
        return JSONResponse(report, status_code=404)
    return JSONResponse(report)


@app.get("/api/book/{symbol}")
async def api_book(symbol: str, levels: int = 20) -> JSONResponse:
    """A deeper ladder than the live socket carries.

    The socket sends eight levels because that is what fits a panel and it goes
    out twenty times a second. A ladder view wants more, and asks for it once.
    """
    venue = runner.market.venue
    instrument = venue.registry.get(symbol)
    if instrument is None:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    snapshot = venue.engine(symbol).book.snapshot(max(1, min(60, levels)))
    return JSONResponse(
        {
            "symbol": symbol,
            "bids": [
                [str(instrument.from_ticks(p)), int(q)] for p, q in snapshot.priced_bids
            ],
            "asks": [
                [str(instrument.from_ticks(p)), int(q)] for p, q in snapshot.priced_asks
            ],
            "indicative": runner.indicative(symbol),
            "session": venue.session(symbol).value,
        }
    )


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


@app.post("/api/config")
async def api_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the market. Starts a fresh session, and says so."""
    return runner.reconfigure(MarketConfig.from_dict(payload or {}))


@app.post("/api/session/{symbol}/halt")
async def api_halt(symbol: str) -> dict[str, Any]:
    return runner.halt(symbol)


@app.post("/api/session/{symbol}/uncross")
async def api_uncross(symbol: str) -> dict[str, Any]:
    return runner.uncross(symbol)


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def stream(socket: WebSocket) -> None:
    await socket.accept()
    # Read before accepting would be tidier, but the cookie is set by the page
    # load that preceded this, so by here it is there or the visitor never
    # loaded the page.
    seat = _seat_for(socket)
    receiver = asyncio.create_task(_receive(socket, seat))
    try:
        while True:
            payload = runner.market.snapshot(seat)
            payload["generation"] = runner.generation
            payload["sessions"] = {
                symbol: runner.market.venue.session(symbol).value
                for symbol in runner.market.venue.registry.symbols
            }
            payload["speed"] = runner.market.speed
            await socket.send_json(payload)
            await asyncio.sleep(TICK_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver


async def _receive(socket: WebSocket, seat: AgentId | None = None) -> None:
    """Handle actions from the browser.

    Orders are queued onto the human agent rather than applied directly, so a
    person's click enters the same event queue as an algorithm's decision and is
    subject to the same latency.
    """
    while True:
        try:
            message = await socket.receive_json()
        except (WebSocketDisconnect, RuntimeError):
            return

        action = message.get("action")
        try:
            if action == "submit":
                price = message.get("price")
                result = runner.market.submit(
                    symbol=message["symbol"],
                    side=message["side"],
                    quantity=int(message["quantity"]),
                    price=None if price in (None, "", "market") else Decimal(str(price)),
                    tif=str(message.get("tif", "")),
                    trader=seat,
                    stop=(
                        None
                        if message.get("stop") in (None, "", "none")
                        else Decimal(str(message["stop"]))
                    ),
                    display=int(message.get("display") or 0),
                )
            elif action == "cancel":
                result = runner.market.cancel(
                    int(message["order_id"]),
                    trader=seat,
                    symbol=message.get("symbol"),
                )
            elif action == "flatten":
                result = runner.market.flatten(trader=seat)
            elif action == "cancel_all":
                result = runner.market.cancel_all(trader=seat)
            elif action == "speed":
                result = runner.set_speed(float(message["value"]))
            else:
                result = {"ok": False, "error": f"unknown action {action!r}"}
        except (KeyError, ValueError, TypeError, InvalidOperation) as bad:
            result = {"ok": False, "error": str(bad)}

        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await socket.send_json({"ack": result})


class _FreshStatic(StaticFiles):
    """Static files that a reload actually re-fetches.

    Browsers cache ES modules hard, and the default headers here let them: a
    change to a view module simply did not appear until someone knew to force a
    reload, which is a miserable way to develop and an easy way to debug the
    wrong version of a file for twenty minutes.

    This is a single-process simulator served over localhost, so revalidating
    every asset costs nothing worth measuring.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:  # noqa: D102
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


if STATIC.is_dir():
    app.mount("/static", _FreshStatic(directory=STATIC), name="static")


def main() -> None:
    """Serve the terminal.

    The port comes from ``PORT`` when it is set, because supervisors -- the
    editor's preview runner among them -- assign one and expect the process to
    take it. Hard-coding 8000 meant a second instance simply refused to start
    against whatever was already holding that port, with no way to redirect it
    short of editing the file.

    An explicit ``--port`` still wins over the environment, since someone who
    typed a port meant it.
    """
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    print(f"Arena Markets -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

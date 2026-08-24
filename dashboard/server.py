"""Serve the live market to a browser.

    python -m dashboard.server
    # then open http://127.0.0.1:8000

A thin shell: it steps the kernel on a timer, pushes snapshots down a WebSocket,
and forwards the human's orders back in. All the behaviour lives in
``arena.market.live`` and the layers below it, so the browser is a viewer of the
simulator rather than a second implementation of it.

Single-process and single-market by design. This is an instrument for watching
and poking at a market, not a service -- a second viewer sees the same market,
which is the useful behaviour when you want to open the book on one screen and
the blotter on another.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dashboard.build_market import build

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# How often the kernel is advanced, and how often a snapshot goes out. 20 Hz is
# well past what an eye resolves and cheap enough that the simulation is never
# waiting on the socket.
TICK_SECONDS = 0.05

app = FastAPI(title="Arena Markets")
market = build()
_pump: asyncio.Task | None = None


async def _run_market() -> None:
    """Advance the kernel in step with the wall clock, forever."""
    market.start()
    while True:
        try:
            market.step()
        except Exception as failure:  # keep the socket alive to report it
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/instruments")
async def api_instruments() -> dict[str, Any]:
    return {
        "instruments": [
            market.venue.registry.require(s).to_dict()
            for s in market.venue.registry.symbols
        ]
    }


@app.websocket("/ws")
async def stream(socket: WebSocket) -> None:
    await socket.accept()
    receiver = asyncio.create_task(_receive(socket))
    try:
        while True:
            await socket.send_json(market.snapshot())
            await asyncio.sleep(TICK_SECONDS)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver


async def _receive(socket: WebSocket) -> None:
    """Handle actions from the browser.

    Orders are queued onto the human agent rather than applied directly, so a
    person's click enters the same event queue as an algorithm's decision and is
    subject to the same latency. A UI that bypassed the kernel would be showing
    you a market you are not actually in.
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
                result = market.submit(
                    symbol=message["symbol"],
                    side=message["side"],
                    quantity=int(message["quantity"]),
                    price=None if price in (None, "", "market") else Decimal(str(price)),
                )
            elif action == "cancel":
                result = market.cancel(int(message["order_id"]))
            elif action == "flatten":
                result = market.flatten()
            elif action == "cancel_all":
                result = market.cancel_all()
            elif action == "speed":
                market.speed = max(0.0, min(20.0, float(message["value"])))
                result = {"ok": True, "speed": market.speed}
            else:
                result = {"ok": False, "error": f"unknown action {action!r}"}
        except (KeyError, ValueError, TypeError, InvalidOperation) as bad:
            result = {"ok": False, "error": str(bad)}

        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await socket.send_json({"ack": result})


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print(f"Arena Markets -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

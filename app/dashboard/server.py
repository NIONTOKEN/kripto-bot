"""
FastAPI dashboard sunucusu.
- GET /           → HTML dashboard
- GET /api/status → Bot durumu
- GET /api/positions → Açık pozisyonlar
- GET /api/trades  → Son 50 işlem
- GET /api/stats   → Günlük istatistikler
- WS /ws           → Anlık güncellemeler (JSON push)
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app.database import (
    Trade,
    get_open_trades,
    get_recent_trades,
    get_todays_closed_pnl,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Kripto Trading Bot", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Bot state referansı (bot.py tarafından set edilir)
_bot_state: Dict[str, Any] = {
    "running": False,
    "balance": 0.0,
    "open_positions": 0,
    "symbols_tracked": 0,
    "uptime_start": datetime.utcnow().isoformat(),
    "testnet": True,
    "daily_pnl": 0.0,
}

# WebSocket bağlantıları
_ws_clients: Set[WebSocket] = set()


def update_bot_state(**kwargs: Any) -> None:
    """Bot.py tarafından çağrılır — state günceller."""
    _bot_state.update(kwargs)


async def broadcast_ws(data: dict) -> None:
    """Tüm bağlı WS istemcilerine mesaj gönder."""
    dead: Set[WebSocket] = set()
    msg = json.dumps(data, default=str)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "side": t.side,
        "entry_price": str(t.entry_price),
        "sl_price": str(t.sl_price) if t.sl_price else None,
        "tp_price": str(t.tp_price) if t.tp_price else None,
        "quantity": str(t.quantity),
        "leverage": t.leverage,
        "pnl": str(t.pnl) if t.pnl else None,
        "pnl_pct": str(t.pnl_pct) if t.pnl_pct else None,
        "status": t.status,
        "close_reason": t.close_reason,
        "signal_score": t.signal_score,
        "ml_confidence": t.ml_confidence,
        "regime": t.regime,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

INDEX_HTML_FILE = TEMPLATES_DIR / "index.html"
MANIFEST_FILE = TEMPLATES_DIR / "manifest.json"
SW_FILE = TEMPLATES_DIR / "sw.js"

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    content = INDEX_HTML_FILE.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/manifest.json")
async def get_manifest():
    from fastapi.responses import Response
    content = MANIFEST_FILE.read_text(encoding="utf-8")
    return Response(content=content, media_type="application/json")


@app.get("/sw.js")
async def get_sw():
    from fastapi.responses import Response
    content = SW_FILE.read_text(encoding="utf-8")
    return Response(content=content, media_type="application/javascript")


@app.get("/api/status")
async def get_status() -> dict:
    return {
        "running": _bot_state["running"],
        "testnet": _bot_state["testnet"],
        "balance_usdt": _bot_state["balance"],
        "open_positions": _bot_state["open_positions"],
        "symbols_tracked": _bot_state["symbols_tracked"],
        "daily_pnl": _bot_state["daily_pnl"],
        "uptime_start": _bot_state["uptime_start"],
        "server_time": datetime.utcnow().isoformat(),
    }


@app.get("/api/positions")
async def get_positions() -> List[dict]:
    trades = await get_open_trades()
    return [_trade_dict(t) for t in trades]


@app.get("/api/trades")
async def get_trades(limit: int = 50) -> List[dict]:
    trades = await get_recent_trades(limit=min(limit, 200))
    return [_trade_dict(t) for t in trades]


@app.get("/api/stats")
async def get_stats() -> dict:
    pnl = await get_todays_closed_pnl()
    trades = await get_recent_trades(100)
    closed = [t for t in trades if t.status == "CLOSED"]
    wins = [t for t in closed if t.pnl and t.pnl > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    return {
        "date": date.today().isoformat(),
        "daily_pnl": str(pnl),
        "closed_trades_today": len(closed),
        "win_rate_pct": round(win_rate, 1),
        "balance": _bot_state["balance"],
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # İlk bağlanınca durum gönder
        await ws.send_text(json.dumps({"type": "status", "data": _bot_state}, default=str))
        while True:
            # İstemciden ping bekle, yoksa 60sn sonra timeout
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)

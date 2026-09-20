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

from app.config import config
from app.database import (
    Signal,
    Trade,
    get_open_trades,
    get_recent_signals,
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
    "symbols_list": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
        "NEARUSDT", "APTUSDT", "PEPEUSDT", "SHIBUSDT", "DOTUSDT",
        "LTCUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "FETUSDT"
    ],
    "uptime_start": datetime.utcnow().isoformat(),
    "testnet": config.BINANCE_TESTNET,
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
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/manifest.json")
async def get_manifest():
    from fastapi.responses import Response
    content = MANIFEST_FILE.read_text(encoding="utf-8")
    return Response(content=content, media_type="application/json")


@app.get("/sw.js")
async def get_sw():
    from fastapi.responses import Response
    content = SW_FILE.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/status")
async def get_status() -> dict:
    return {
        "running": _bot_state["running"],
        "testnet": _bot_state["testnet"],
        "balance_usdt": _bot_state["balance"],
        "wallet_balance": _bot_state.get("wallet_balance", _bot_state["balance"]),
        "unrealized_pnl": _bot_state.get("unrealized_pnl", 0.0),
        "open_positions": _bot_state["open_positions"],
        "symbols_tracked": _bot_state["symbols_tracked"],
        "daily_pnl": _bot_state["daily_pnl"],
        "uptime_start": _bot_state["uptime_start"],
        "error": _bot_state.get("error", None),
        "server_time": datetime.utcnow().isoformat(),
    }


@app.get("/api/positions")
async def get_positions() -> List[dict]:
    trades = await get_open_trades()
    res = []
    
    # Canlı fiyat ve PnL verisi almak için bot instance'ına bak
    from app.bot import get_bot
    bot = get_bot()
    live_positions = {}
    if bot and bot.client:
        try:
            positions_data = await bot.client.get_positions()
            for p in positions_data:
                live_positions[p["symbol"]] = p
        except Exception:
            pass

    for t in trades:
        d = _trade_dict(t)
        pos = live_positions.get(t.symbol)
        if pos:
            unrealized = float(pos.get("unrealizedProfit", pos.get("unRealizedProfit", "0")))
            mark_price = float(pos.get("markPrice", "0"))
            entry_price = float(t.entry_price or pos.get("entryPrice", "1"))
            margin = (float(t.quantity) * entry_price) / float(t.leverage or 7)
            pnl_pct = (unrealized / margin * 100) if margin > 0 else 0.0
            
            d["live_pnl"] = round(unrealized, 4)
            d["live_pnl_pct"] = round(pnl_pct, 2)
            d["mark_price"] = mark_price
        else:
            d["live_pnl"] = 0.0
            d["live_pnl_pct"] = 0.0
            d["mark_price"] = float(t.entry_price or 0)
        res.append(d)
    return res


@app.get("/api/trades")
async def get_trades(limit: int = 50) -> List[dict]:
    trades = await get_recent_trades(limit=min(limit, 200))
    return [_trade_dict(t) for t in trades]


@app.get("/api/stats")
async def get_stats() -> dict:
    pnl = await get_todays_closed_pnl()
    trades = await get_recent_trades(200)
    closed = [t for t in trades if t.status == "CLOSED"]
    wins = [t for t in closed if t.pnl and t.pnl > 0]
    losses = [t for t in closed if t.pnl and t.pnl <= 0]
    long_trades = [t for t in closed if t.side == "LONG"]
    short_trades = [t for t in closed if t.side == "SHORT"]
    long_wins = [t for t in long_trades if t.pnl and t.pnl > 0]
    short_wins = [t for t in short_trades if t.pnl and t.pnl > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    total_profit = sum(float(t.pnl) for t in wins) if wins else 0.0
    total_loss = sum(float(t.pnl) for t in losses) if losses else 0.0
    balance = float(_bot_state.get("balance", 0))
    balance_start = balance - float(pnl)
    wallet_pct = (float(pnl) / balance_start * 100) if balance_start > 0 else 0.0

    # Per-trade list for modal detail
    trade_list = []
    for t in closed:
        trade_list.append({
            "symbol": t.symbol,
            "side": t.side,
            "entry_price": str(t.entry_price),
            "close_price": str(t.close_price) if hasattr(t, "close_price") and t.close_price else None,
            "pnl": str(t.pnl) if t.pnl else "0",
            "pnl_pct": str(t.pnl_pct) if t.pnl_pct else "0",
            "close_reason": t.close_reason,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "leverage": t.leverage,
            "quantity": str(t.quantity),
        })
    
    return {
        "date": date.today().isoformat(),
        "daily_pnl": str(pnl),
        "closed_trades_today": len(closed),
        "total_closed_all": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "long_count": len(long_trades),
        "short_count": len(short_trades),
        "long_wins": len(long_wins),
        "short_wins": len(short_wins),
        "long_losses": len(long_trades) - len(long_wins),
        "short_losses": len(short_trades) - len(short_wins),
        "win_rate_pct": round(win_rate, 1),
        "total_profit": round(total_profit, 4),
        "total_loss": round(total_loss, 4),
        "balance": balance,
        "wallet_change_pct": round(wallet_pct, 2),
        "trades": trade_list,
    }



@app.get("/api/symbols")
async def get_symbols() -> List[str]:
    """Takip edilen veya taranan sembollerin listesini döner."""
    return _bot_state.get("symbols_list", [])


@app.get("/api/signals")
async def get_signals(limit: int = 30) -> List[dict]:
    """Son üretilen AI sinyal analizlerini döner."""
    signals = await get_recent_signals(limit=limit)
    return [
        {
            "id": s.id,
            "symbol": s.symbol,
            "timestamp": s.timestamp.isoformat() if s.timestamp else None,
            "direction": s.direction,
            "score": s.score,
            "ml_confidence": s.ml_confidence,
            "regime": s.regime,
            "rsi": s.rsi,
            "macd_hist": s.macd_hist,
            "adx": s.adx,
            "atr": s.atr,
        }
        for s in signals
    ]


@app.get("/api/orderbook-radar")
async def get_orderbook_radar() -> List[dict]:
    """
    Takip edilen en aktif paritelerin canlı tahta derinliği, 
    balina duvarları ve alış/satış dengesini döner.
    """
    from app.bot import get_bot
    from app.strategy.orderbook import analyze_order_book
    bot = get_bot()
    if not bot or not bot.client:
        return []

    symbols = bot._symbols[:12] if hasattr(bot, "_symbols") and bot._symbols else ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    results = []

    for sym in symbols:
        try:
            depth = await bot.client.get_order_book(sym, limit=20)
            mark_price = float(await bot.client.get_mark_price(sym))
            ob = analyze_order_book(depth, mark_price)
            results.append({
                "symbol": sym,
                "price": mark_price,
                "imbalance": ob.imbalance,
                "imbalance_pct": round(ob.imbalance * 100, 1),
                "bid_vol": ob.bid_volume_usdt,
                "ask_vol": ob.ask_volume_usdt,
                "bias": ob.bias,
                "has_bid_wall": ob.has_bid_wall,
                "has_ask_wall": ob.has_ask_wall,
                "bid_wall_price": ob.bid_wall_price,
                "ask_wall_price": ob.ask_wall_price,
                "signal_score": ob.signal_score,
            })
        except Exception:
            continue

    return results


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

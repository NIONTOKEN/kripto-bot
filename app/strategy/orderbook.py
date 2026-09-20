"""
Order Book (Emir Defteri & Derinlik) Analiz Motoru.
Milisaniyelik tahta dengesizliği ve balina duvarlarını tespit eder.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple

@dataclass
class OrderBookAnalysis:
    imbalance: float       # -1.0 (aşırı satıcı) ile +1.0 (aşırı alıcı)
    bid_volume_usdt: float # Alış tarafındaki toplam dolar hacmi
    ask_volume_usdt: float # Satış tarafındaki toplam dolar hacmi
    has_bid_wall: bool     # Alış tarafında balina duvarı var mı?
    has_ask_wall: bool     # Satış tarafında balina duvarı var mı?
    bid_wall_price: float  # En yakın alış duvarı fiyatı
    ask_wall_price: float  # En yakın satış duvarı fiyatı
    signal_score: float    # 0 ile 100 arası tahta güven skoru
    bias: str              # 'BULLISH', 'BEARISH', 'NEUTRAL'

def analyze_order_book(depth: Dict, current_price: float) -> OrderBookAnalysis:
    """
    Binance depth verisini (bids, asks) analiz eder.
    depth = {'bids': [[price, qty], ...], 'asks': [[price, qty], ...]}
    """
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])

    if not bids or not asks:
        return OrderBookAnalysis(0.0, 0.0, 0.0, False, False, 0.0, 0.0, 0.0, "NEUTRAL")

    # İlk 25 kademeyi incele
    bids_subset = bids[:25]
    asks_subset = asks[:25]

    bid_vols = []
    ask_vols = []
    bid_total_usdt = 0.0
    ask_total_usdt = 0.0

    for p, q in bids_subset:
        price = float(p)
        qty = float(q)
        val = price * qty
        bid_vols.append((price, val))
        bid_total_usdt += val

    for p, q in asks_subset:
        price = float(p)
        qty = float(q)
        val = price * qty
        ask_vols.append((price, val))
        ask_total_usdt += val

    # Dengesizlik Oranı (Imbalance): -1 ile +1 arası
    total_vol = bid_total_usdt + ask_total_usdt
    if total_vol > 0:
        imbalance = (bid_total_usdt - ask_total_usdt) / total_vol
    else:
        imbalance = 0.0

    # Balina Duvarı Tespiti (Ortalama kademe büyüklüğünün 3 katı)
    avg_bid_val = bid_total_usdt / max(len(bid_vols), 1)
    avg_ask_val = ask_total_usdt / max(len(ask_vols), 1)

    has_bid_wall = False
    bid_wall_price = 0.0
    for price, val in bid_vols:
        if val > avg_bid_val * 3.0 and val >= 10000.0:  # En az 10.000$ veya 3x
            has_bid_wall = True
            bid_wall_price = price
            break

    has_ask_wall = False
    ask_wall_price = 0.0
    for price, val in ask_vols:
        if val > avg_ask_val * 3.0 and val >= 10000.0:
            has_ask_wall = True
            ask_wall_price = price
            break

    # Yön ve Güven Skoru
    bias = "NEUTRAL"
    score = 0.0

    if imbalance > 0.35 and not has_ask_wall:
        # Alıcılar baskın ve satış duvarı yok -> Güçlü Yükseliş Beklentisi
        bias = "BULLISH"
        score = min(100.0, (imbalance * 80.0) + (20.0 if has_bid_wall else 0.0))
    elif imbalance < -0.35 and not has_bid_wall:
        # Satıcılar baskın ve alış duvarı yok -> Güçlü Düşüş Beklentisi
        bias = "BEARISH"
        score = min(100.0, (abs(imbalance) * 80.0) + (20.0 if has_ask_wall else 0.0))

    return OrderBookAnalysis(
        imbalance=round(imbalance, 3),
        bid_volume_usdt=round(bid_total_usdt, 2),
        ask_volume_usdt=round(ask_total_usdt, 2),
        has_bid_wall=has_bid_wall,
        has_ask_wall=has_ask_wall,
        bid_wall_price=bid_wall_price,
        ask_wall_price=ask_wall_price,
        signal_score=round(score, 1),
        bias=bias,
    )

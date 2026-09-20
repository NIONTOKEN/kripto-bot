"""
Order Book + Trend Hibrit Sinyal Motoru.
Önce piyasa rejimini belirle: BULL → SADECE LONG, BEAR → SADECE SHORT.
Counter-trend işlem YASAK.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from app.config import config
from app.strategy.indicators import IndicatorResult
from app.strategy.regime import Regime
from app.strategy.orderbook import OrderBookAnalysis
from app.strategy.alphapulse import calculate_alphapulse, AlphaPulseResult

@dataclass
class SignalResult:
    direction: str
    score: float
    ml_confidence: float
    regime: Regime
    ema_contrib: float
    rsi_contrib: float
    macd_contrib: float
    bb_contrib: float
    volume_contrib: float
    adx_contrib: float
    signal_type: str = "NORMAL"  # SUPER_LONG, COLLAPSE_SHORT, INTRADAY_LONG, NORMAL, SCALP_LONG, SCALP_SHORT
    alpha_score: float = 50.0

def calculate_signal(
    ind: IndicatorResult,
    ml_confidence: float,
    regime: Regime,
    ob: Optional[OrderBookAnalysis] = None,
    btc_bullish: Optional[bool] = None,
    trend_1h: str = "NEUTRAL",
    trend_4h: str = "NEUTRAL",
    candles: Optional[list[dict]] = None,
    funding_rate: float = 0.0,
    open_interest: float = 0.0,
) -> SignalResult:
    ml = ml_confidence if ml_confidence is not None else 0.5

    # 1. EMA Trend Skoru (-1 ile +1)
    ema_score = 0.0
    if ind.close > ind.ema200 and ind.ema9 > ind.ema21:
        ema_score = 0.8
    elif ind.close < ind.ema200 and ind.ema9 < ind.ema21:
        ema_score = -0.8
    elif ind.close > ind.ema50:
        ema_score = 0.3
    elif ind.close < ind.ema50:
        ema_score = -0.3

    # 2. MACD Momentum
    macd_score = 0.0
    if ind.atr > 0:
        norm_macd = ind.macd_hist / ind.atr
        macd_score = max(-1.0, min(1.0, norm_macd * 2.0))

    # 3. ADX Gücü
    adx_score = 0.0
    if ind.adx >= 20:
        if ind.adx_pos > ind.adx_neg:
            adx_score = min(1.0, (ind.adx - 20) / 25.0)
        else:
            adx_score = -min(1.0, (ind.adx - 20) / 25.0)

    # 4. ORDER BOOK (EMİR DEFTERİ DENGESİ) — EN YÜKSEK AĞIRLIK! (%45)
    ob_score = 0.0
    if ob is not None:
        if ob.bias == "BULLISH":
            ob_score = (ob.signal_score / 100.0)
        elif ob.bias == "BEARISH":
            ob_score = -(ob.signal_score / 100.0)

    # Hibrit Skor Hesabı: Order Book %45 + EMA %25 + MACD %15 + ADX %15
    if ob is not None and ob.bias != "NEUTRAL":
        total = (ob_score * 0.45) + (ema_score * 0.25) + (macd_score * 0.15) + (adx_score * 0.15)
    else:
        total = (ema_score * 0.40) + (macd_score * 0.30) + (adx_score * 0.30)

    direction = "NEUTRAL"
    score = 0.0
    signal_type = "NORMAL"

    # ── KURUMSAL TREND & MAKRO ÇOKLU ZAMAN DİLİMİ (4H + 1H + 5M) ─────────────
    above_ema200 = ind.close >= ind.ema200
    below_ema200 = ind.close <= ind.ema200

    # 💀 A) COLLAPSE_SHORT (LUNA / Makro Çöküş & Ölüm Sarmalı Avcısı):
    # Bir coin hem 4H'de hem 1H'de çökmüşse, 200 EMA altında ve satış baskısı altındaysa:
    # Bu coine özel SHORT açılır ve günlerce/haftalarca dibe kadar sürülür!
    is_death_spiral = (
        trend_4h == "BEARISH" and
        trend_1h == "BEARISH" and
        below_ema200 and
        (regime in (Regime.BEAR_TREND, Regime.VOLATILE, Regime.RANGING)) and
        (total < -0.22) and
        (not (ob and ob.has_bid_wall)) and
        (ind.rsi < 60)
    )

    # 🚀 B) SUPER_LONG (Parabolik Trend & Süper Boğa Adayı):
    # Hem 4H hem 1H Bullish, 200 EMA üstünde ve güçlü alım:
    is_super_bull = (
        trend_4h == "BULLISH" and
        trend_1h == "BULLISH" and
        above_ema200 and
        (regime != Regime.BEAR_TREND) and
        (total > 0.22) and
        (not (ob and ob.has_ask_wall)) and
        (ind.rsi > 38 and ind.rsi < 72)
    )

    # 📈 C) INTRADAY_LONG (Normal Gün İçi Trend Long):
    allow_regular_long = (
        trend_1h == "BULLISH" and
        above_ema200 and
        (regime != Regime.BEAR_TREND) and
        (btc_bullish is not False) and
        (total > 0.24) and
        (not (ob and ob.has_ask_wall)) and
        (ind.rsi > 36 and ind.rsi < 68)
    )

    # 📉 D) INTRADAY_SHORT (Normal Gün İçi Trend Short):
    allow_regular_short = (
        trend_1h == "BEARISH" and
        below_ema200 and
        (regime != Regime.BULL_TREND) and
        (btc_bullish is not True) and
        (total < -0.24) and
        (not (ob and ob.has_bid_wall)) and
        (ind.rsi > 32 and ind.rsi < 64)
    )

    # ── 10 Faktörlü AlphaPulse Matrix Analizi ────────────────────────────────
    alpha_res = calculate_alphapulse(
        ind=ind,
        candles=candles or [],
        ob=ob,
        funding_rate=funding_rate,
        open_interest=open_interest,
        trend_1h=trend_1h,
        trend_4h=trend_4h,
    )

    # ⚡ E) SCALP_LONG (Hızlı Vur-Kaç Long - Anlık Alıcı Baskısı & Tahta Dengesizliği):
    allow_scalp_long = (
        config.SCALP_MODE and
        (ob is not None and ob.imbalance >= 0.18) and
        (total > 0.18 or alpha_res.alpha_score >= 62.0) and
        (not (ob and ob.has_ask_wall)) and
        (ind.rsi < 75)
    )

    # ⚡ F) SCALP_SHORT (Hızlı Vur-Kaç Short - Anlık Satıcı Baskısı & Tahta Dengesizliği):
    allow_scalp_short = (
        config.SCALP_MODE and
        (ob is not None and ob.imbalance <= -0.18) and
        (total < -0.18 or alpha_res.alpha_score <= 38.0) and
        (not (ob and ob.has_bid_wall)) and
        (ind.rsi > 25)
    )

    # Karar Motoru Öncelik Sıralaması:
    if is_death_spiral:
        direction = "SHORT"
        score = min(100.0, abs(total) * 110)
        signal_type = "COLLAPSE_SHORT"
    elif is_super_bull:
        direction = "LONG"
        score = min(100.0, total * 110)
        signal_type = "SUPER_LONG"
    elif alpha_res.bias == "STRONG_BUY" and (not (ob and ob.has_ask_wall)):
        direction = "LONG"
        score = min(100.0, alpha_res.alpha_score * 1.1)
        signal_type = "SCALP_LONG" if (ob and ob.imbalance >= 0.15) else "INTRADAY_LONG"
    elif alpha_res.bias == "STRONG_SELL" and (not (ob and ob.has_bid_wall)):
        direction = "SHORT"
        score = min(100.0, (100.0 - alpha_res.alpha_score) * 1.1)
        signal_type = "SCALP_SHORT" if (ob and ob.imbalance <= -0.15) else "INTRADAY_SHORT"
    elif allow_scalp_long and total > 0.25:
        direction = "LONG"
        score = min(100.0, total * 115)
        signal_type = "SCALP_LONG"
    elif allow_scalp_short and total < -0.25:
        direction = "SHORT"
        score = min(100.0, abs(total) * 115)
        signal_type = "SCALP_SHORT"
    elif allow_regular_long:
        direction = "LONG"
        score = min(100.0, total * 100)
        signal_type = "INTRADAY_LONG"
    elif allow_regular_short:
        direction = "SHORT"
        score = min(100.0, abs(total) * 100)
        signal_type = "INTRADAY_SHORT"
    elif allow_scalp_long:
        direction = "LONG"
        score = min(100.0, total * 105)
        signal_type = "SCALP_LONG"
    elif allow_scalp_short:
        direction = "SHORT"
        score = min(100.0, abs(total) * 105)
        signal_type = "SCALP_SHORT"
    else:
        direction = "NEUTRAL"
        score = 0.0
        signal_type = "NORMAL"

    # Sadece LONG modu kontrolü
    if config.ONLY_LONG and direction == "SHORT" and signal_type != "COLLAPSE_SHORT":
        direction = "NEUTRAL"
        score = 0.0
        signal_type = "NORMAL"

    score_threshold = config.MIN_SCORE_TO_OPEN
    if signal_type in ("SUPER_LONG", "COLLAPSE_SHORT"):
        score_threshold = min(config.MIN_SCORE_TO_OPEN, 40.0)
    elif "SCALP" in signal_type:
        score_threshold = min(config.MIN_SCORE_TO_OPEN, 38.0)
    elif regime in (Regime.RANGING, Regime.VOLATILE):
        score_threshold = config.MIN_SCORE_TO_OPEN * 1.10

    if score < score_threshold:
        direction = "NEUTRAL"
        signal_type = "NORMAL"

    return SignalResult(
        direction=direction,
        score=round(score, 2),
        ml_confidence=round(ml, 4),
        regime=regime,
        ema_contrib=round(ema_score, 2),
        rsi_contrib=round(ind.rsi, 1),
        macd_contrib=round(macd_score, 2),
        bb_contrib=round(ob.imbalance if ob else 0.0, 2),
        volume_contrib=round(ind.volume_ratio, 2),
        adx_contrib=round(adx_score, 2),
        signal_type=signal_type,
        alpha_score=alpha_res.alpha_score,
    )

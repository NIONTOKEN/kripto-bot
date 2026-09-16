"""
Teknik indikatörler — pandas + ta kütüphanesi kullanılır.
Giriş: list of dicts (MarketDataStore formatı)
Çıkış: IndicatorResult dataclass
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd
import ta


@dataclass
class IndicatorResult:
    # Son kapanış değerleri
    close: float
    high: float
    low: float
    volume: float

    # EMA'lar
    ema9: float
    ema21: float
    ema50: float
    ema200: float

    # RSI
    rsi: float

    # MACD
    macd: float
    macd_signal: float
    macd_hist: float

    # Bollinger Bands
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_pct_b: float       # 0 = alt bant, 1 = üst bant

    # ATR
    atr: float
    atr_pct: float        # atr / close

    # ADX
    adx: float
    adx_pos: float        # +DI
    adx_neg: float        # -DI

    # Stochastic RSI
    stoch_k: float
    stoch_d: float

    # VWAP
    vwap: float
    price_vs_vwap: float  # (close - vwap) / vwap

    # Hacim
    volume_sma20: float
    volume_ratio: float   # volume / volume_sma20

    # Hesaplama başarılı mı?
    valid: bool = True


def _safe_last(series: pd.Series) -> float:
    """Serinin son geçerli değerini döner, yoksa NaN."""
    vals = series.dropna()
    if vals.empty:
        return float("nan")
    return float(vals.iloc[-1])


def calculate_indicators(candles: List[dict]) -> Optional[IndicatorResult]:
    """
    MarketDataStore formatındaki mum listesinden indikatör hesaplar.
    En az 210 mum gerekir (EMA200 için).
    Yetersiz veri veya NaN varsa None döner.
    """
    if len(candles) < 210:
        return None

    df = pd.DataFrame(candles)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    # ── EMA ──────────────────────────────────────────────────────────────────
    ema9 = _safe_last(ta.trend.ema_indicator(close, window=9))
    ema21 = _safe_last(ta.trend.ema_indicator(close, window=21))
    ema50 = _safe_last(ta.trend.ema_indicator(close, window=50))
    ema200 = _safe_last(ta.trend.ema_indicator(close, window=200))

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi_series = ta.momentum.rsi(close, window=14)
    rsi = _safe_last(rsi_series)

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_ind = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd_val = _safe_last(macd_ind.macd())
    macd_sig = _safe_last(macd_ind.macd_signal())
    macd_hist = _safe_last(macd_ind.macd_diff())

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = _safe_last(bb.bollinger_hband())
    bb_middle = _safe_last(bb.bollinger_mavg())
    bb_lower = _safe_last(bb.bollinger_lband())
    bb_pct_b = _safe_last(bb.bollinger_pband())

    # ── ATR ──────────────────────────────────────────────────────────────────
    atr_series = ta.volatility.average_true_range(high, low, close, window=14)
    atr = _safe_last(atr_series)

    # ── ADX ──────────────────────────────────────────────────────────────────
    adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
    adx = _safe_last(adx_ind.adx())
    adx_pos = _safe_last(adx_ind.adx_pos())
    adx_neg = _safe_last(adx_ind.adx_neg())

    # ── Stochastic RSI ───────────────────────────────────────────────────────
    stoch = ta.momentum.StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
    stoch_k = _safe_last(stoch.stochrsi_k()) * 100  # ta döner 0-1, biz 0-100 isteriz
    stoch_d = _safe_last(stoch.stochrsi_d()) * 100

    # ── VWAP ─────────────────────────────────────────────────────────────────
    try:
        vwap_series = ta.volume.volume_weighted_average_price(
            high=high, low=low, close=close, volume=volume, window=14
        )
        vwap = _safe_last(vwap_series)
    except Exception:
        vwap = float("nan")

    # ── Hacim SMA ────────────────────────────────────────────────────────────
    vol_sma20 = _safe_last(volume.rolling(20).mean())
    last_close = _safe_last(close)
    last_volume = float(volume.iloc[-1])

    # NaN kontrolü
    critical = [ema9, ema21, ema50, ema200, rsi, macd_hist, bb_upper, bb_lower, atr, adx]
    if any(np.isnan(v) for v in critical):
        return None
    if np.isnan(vwap):
        vwap = last_close  # fallback
    if np.isnan(vol_sma20) or vol_sma20 == 0:
        vol_sma20 = last_volume

    atr_pct = atr / last_close if last_close > 0 else 0.0
    price_vs_vwap = (last_close - vwap) / vwap if vwap > 0 else 0.0
    volume_ratio = last_volume / vol_sma20 if vol_sma20 > 0 else 1.0

    return IndicatorResult(
        close=last_close,
        high=float(high.iloc[-1]),
        low=float(low.iloc[-1]),
        volume=last_volume,
        ema9=ema9,
        ema21=ema21,
        ema50=ema50,
        ema200=ema200,
        rsi=rsi,
        macd=macd_val,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_pct_b=bb_pct_b if not np.isnan(bb_pct_b) else 0.5,
        atr=atr,
        atr_pct=atr_pct,
        adx=adx,
        adx_pos=adx_pos,
        adx_neg=adx_neg,
        stoch_k=stoch_k if not np.isnan(stoch_k) else 50.0,
        stoch_d=stoch_d if not np.isnan(stoch_d) else 50.0,
        vwap=vwap,
        price_vs_vwap=price_vs_vwap,
        volume_sma20=vol_sma20,
        volume_ratio=volume_ratio,
        valid=True,
    )

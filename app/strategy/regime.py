"""
Piyasa rejimi tespiti.
Trending / Ranging / Volatile / Bear Trend kategorileri.
"""
from __future__ import annotations

from enum import Enum

from app.strategy.indicators import IndicatorResult


class Regime(str, Enum):
    BULL_TREND = "BULL_TREND"       # Güçlü yukarı trend
    BEAR_TREND = "BEAR_TREND"       # Güçlü aşağı trend
    RANGING = "RANGING"             # Yatay hareket
    VOLATILE = "VOLATILE"           # Yüksek volatilite, belirsiz


def detect_regime(ind: IndicatorResult) -> Regime:
    """
    ADX, EMA hizalaması ve ATR% kullanarak piyasa rejimini belirle.
    """
    adx = ind.adx
    atr_pct = ind.atr_pct

    # Yüksek volatilite eşiği: ATR > %3
    if atr_pct > 0.03:
        return Regime.VOLATILE

    # Güçlü trend: ADX > 25
    if adx >= 25:
        # EMA hizalamasına göre yön belirle
        ema_bull_aligned = ind.ema9 > ind.ema21 > ind.ema50
        ema_bear_aligned = ind.ema9 < ind.ema21 < ind.ema50
        adx_bull = ind.adx_pos > ind.adx_neg
        adx_bear = ind.adx_neg > ind.adx_pos

        if ema_bull_aligned and adx_bull:
            return Regime.BULL_TREND
        if ema_bear_aligned and adx_bear:
            return Regime.BEAR_TREND
        # ADX yüksek ama EMA karışık → volatile say
        return Regime.VOLATILE

    # ADX < 25: yatay piyasa
    return Regime.RANGING


def regime_multiplier(regime: Regime) -> float:
    """
    Sinyal skoru çarpanı — riskli rejimlerde skoru azalt.
    BULL/BEAR TREND → tam puan
    RANGING        → %70 (yatay piyasada düşük güven)
    VOLATILE       → %50 (yüksek volatilite, düşük güven)
    """
    return {
        Regime.BULL_TREND: 1.0,
        Regime.BEAR_TREND: 1.0,
        Regime.RANGING: 0.70,
        Regime.VOLATILE: 0.50,
    }[regime]

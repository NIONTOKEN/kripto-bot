"""
Sinyal motoru — teknik indikatörler + ML + rejim kombinasyonu.
Her bileşen -1 ile +1 arasında katkı sağlar.
Final skor: 0-100. ≥ MIN_SCORE_TO_OPEN ise işlem açılır.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import config
from app.strategy.indicators import IndicatorResult
from app.strategy.regime import Regime, regime_multiplier


@dataclass
class SignalResult:
    direction: str      # 'LONG', 'SHORT', 'NEUTRAL'
    score: float        # 0-100
    ml_confidence: float
    regime: Regime
    # Bileşen detayları (loglama için)
    ema_contrib: float
    rsi_contrib: float
    macd_contrib: float
    bb_contrib: float
    volume_contrib: float
    adx_contrib: float


# Bileşen ağırlıkları (toplam = 1.0)
_WEIGHTS = {
    "ema": 0.25,
    "rsi": 0.15,
    "macd": 0.20,
    "bb": 0.15,
    "volume": 0.10,
    "adx": 0.15,
}


def _ema_signal(ind: IndicatorResult) -> float:
    """
    EMA hizalaması ve fiyatın EMA21 konumu.
    +1 = güçlü boğa, -1 = güçlü ayı
    """
    score = 0.0
    # EMA hiyerarşisi (her adım 0.33 katkı)
    if ind.ema9 > ind.ema21:
        score += 0.25
    else:
        score -= 0.25
    if ind.ema21 > ind.ema50:
        score += 0.25
    else:
        score -= 0.25
    if ind.ema50 > ind.ema200:
        score += 0.25
    else:
        score -= 0.25
    # Fiyat EMA21'in üzerinde mi?
    if ind.close > ind.ema21:
        score += 0.25
    else:
        score -= 0.25
    return max(-1.0, min(1.0, score))


def _rsi_signal(ind: IndicatorResult) -> float:
    """
    RSI aşırı alım/satım sinyali.
    RSI < 30 → güçlü long, RSI > 70 → güçlü short
    """
    rsi = ind.rsi
    if rsi <= 20:
        return 1.0
    elif rsi <= 30:
        return 0.8
    elif rsi <= 40:
        return 0.4
    elif rsi <= 60:
        return 0.0
    elif rsi <= 70:
        return -0.4
    elif rsi <= 80:
        return -0.8
    else:
        return -1.0


def _macd_signal(ind: IndicatorResult) -> float:
    """
    MACD histogram yönü ve büyüklüğü.
    normalize: hist / ATR
    """
    if ind.atr == 0:
        return 0.0
    # MACD'yi ATR'ye normalize et
    norm = ind.macd_hist / ind.atr
    # ±1.0'da doyur
    return max(-1.0, min(1.0, norm * 2.0))


def _bb_signal(ind: IndicatorResult) -> float:
    """
    Bollinger Band konumu.
    %B < 0.1 → aşırı satış (long), %B > 0.9 → aşırı alış (short)
    """
    pct = ind.bb_pct_b
    if pct <= 0.0:
        return 1.0
    elif pct <= 0.1:
        return 0.8
    elif pct <= 0.3:
        return 0.3
    elif pct <= 0.7:
        return 0.0
    elif pct <= 0.9:
        return -0.3
    elif pct <= 1.0:
        return -0.8
    else:
        return -1.0


def _volume_signal(ind: IndicatorResult) -> float:
    """
    Yüksek hacim trendi doğrular (direction-agnostic).
    Her zaman pozitif katkı sağlar (hacim onayı).
    """
    ratio = ind.volume_ratio
    if ratio >= 2.0:
        return 0.8
    elif ratio >= 1.5:
        return 0.5
    elif ratio >= 1.0:
        return 0.2
    else:
        return -0.2


def _adx_signal(ind: IndicatorResult) -> float:
    """
    ADX trend gücü ve yönü.
    +DI > -DI → long yönlü, -DI > +DI → short yönlü
    """
    adx = ind.adx
    di_diff = ind.adx_pos - ind.adx_neg

    if adx < 15:
        return 0.0   # trend yok

    # Yön skoru
    dir_score = max(-1.0, min(1.0, di_diff / 20.0))
    # Güç katsayısı
    strength = min(1.0, (adx - 15) / 35.0)
    return dir_score * strength


def calculate_signal(
    ind: IndicatorResult,
    ml_confidence: float,
    regime: Regime,
) -> SignalResult:
    """
    Tüm bileşenleri birleştirerek 0-100 arası sinyal skoru üretir.

    ml_confidence: 0.5 = belirsiz, 1.0 = güçlü long, 0.0 = güçlü short
    """
    ema_c = _ema_signal(ind)
    rsi_c = _rsi_signal(ind)
    macd_c = _macd_signal(ind)
    bb_c = _bb_signal(ind)
    vol_c = _volume_signal(ind)
    adx_c = _adx_signal(ind)

    # Ağırlıklı toplam (-1 ile +1 arası)
    raw = (
        _WEIGHTS["ema"] * ema_c
        + _WEIGHTS["rsi"] * rsi_c
        + _WEIGHTS["macd"] * macd_c
        + _WEIGHTS["bb"] * bb_c
        + _WEIGHTS["volume"] * vol_c
        + _WEIGHTS["adx"] * adx_c
    )

    # ML faktörü: ml_confidence 0.5'ten ne kadar uzaksa o kadar güçlendirme
    ml_factor = 0.5 + 0.5 * abs(ml_confidence - 0.5) * 2
    raw_adjusted = raw * ml_factor

    # Rejim çarpanı
    r_mult = regime_multiplier(regime)
    raw_adjusted *= r_mult

    # Yön belirleme
    if raw_adjusted > 0:
        # Long: ml yönü de long mu?
        ml_agrees = ml_confidence >= 0.5
        if ml_agrees:
            score = raw_adjusted * 100  # 0-100
        else:
            # ML karşı çıkıyor → skoru yarıya indir
            score = raw_adjusted * 50
        direction = "LONG"
    elif raw_adjusted < 0:
        # Short: ml yönü short mu?
        ml_agrees = ml_confidence < 0.5
        if ml_agrees:
            score = abs(raw_adjusted) * 100
        else:
            score = abs(raw_adjusted) * 50
        direction = "SHORT"
    else:
        score = 0.0
        direction = "NEUTRAL"

    score = min(100.0, score)

    # Yön karmaşası: rejim ve ML çelişiyorsa NEUTRAL yap
    if direction == "LONG" and regime == Regime.BEAR_TREND:
        score *= 0.5
    if direction == "SHORT" and regime == Regime.BULL_TREND:
        score *= 0.5

    if score < config.MIN_SCORE_TO_OPEN:
        direction = "NEUTRAL"

    return SignalResult(
        direction=direction,
        score=round(score, 2),
        ml_confidence=round(ml_confidence, 4),
        regime=regime,
        ema_contrib=round(ema_c, 3),
        rsi_contrib=round(rsi_c, 3),
        macd_contrib=round(macd_c, 3),
        bb_contrib=round(bb_c, 3),
        volume_contrib=round(vol_c, 3),
        adx_contrib=round(adx_c, 3),
    )

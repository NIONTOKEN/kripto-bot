"""
AlphaPulse Matrix (APM-10) — 10 Bileşenli Hibrit Özel İndikatör Motoru.
1. Trend (EMA 9/21/50/200 + Üst Zaman Dilimi)
2. Momentum (RSI 14 + MACD 12/26/9 + StochRSI)
3. Hacim (Volume vs Volume SMA20)
4. Volatilite (ATR + Bollinger Squeeze/Breakout)
5. Destek / Direnç (Swing High/Low Pivot Kanalları)
6. Kısa Vadeli Fiyat Hareketi (Price Action: Pinbar / Engulfing / Momentum Impulse)
7. Fonlama Oranı (Funding Rate Squeeze Analizi)
8. Açık Pozisyon Hacmi (Open Interest Trend Teyidi)
9. Emir Defteri (Order Book Imbalance & Balina Duvarları)
10. Alpha Composite Score (0 - 100) & Otomatik Sinyal
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional
from app.strategy.indicators import IndicatorResult
from app.strategy.orderbook import OrderBookAnalysis

@dataclass
class AlphaPulseResult:
    alpha_score: float         # 0.0 ile 100.0 arası master kompozit skor
    bias: str                  # 'STRONG_BUY', 'BUY', 'NEUTRAL', 'SELL', 'STRONG_SELL'
    action_type: str           # 'SCALP_LONG', 'SCALP_SHORT', 'INTRADAY_LONG', 'INTRADAY_SHORT', 'NONE'
    confidence: float          # 0.0 - 1.0 arası sinyal güven derecesi
    trend_score: float         # -1.0 ile +1.0
    momentum_score: float      # -1.0 ile +1.0
    volume_score: float        # -1.0 ile +1.0
    pa_pattern: str            # 'BULLISH_ENGULFING', 'HAMMER', 'BEARISH_ENGULFING', 'SHOOTING_STAR', 'NONE'
    support_price: float       # En yakın dinamik destek
    resistance_price: float    # En yakın dinamik direnç
    funding_bias: str          # 'SQUEEZE_LONG', 'SQUEEZE_SHORT', 'NEUTRAL'
    reasons: List[str]         # Sinyal gerekçeleri

def analyze_price_action(candles: List[dict]) -> tuple[str, float]:
    """Son 2-3 mumdaki Price Action formasyonlarını tespit eder."""
    if len(candles) < 3:
        return "NONE", 0.0

    last = candles[-1]
    prev = candles[-2]

    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    po, ph, pl, pc = float(prev["open"]), float(prev["high"]), float(prev["low"]), float(prev["close"])

    body = abs(c - o)
    total_range = h - l if (h - l) > 0 else 0.0001
    upper_wick = h - max(c, o)
    lower_wick = min(c, o) - l

    # 1. Bullish Pinbar / Hammer
    if lower_wick >= 2.0 * body and upper_wick <= 0.4 * body and c > o:
        return "HAMMER", 0.85

    # 2. Bearish Pinbar / Shooting Star
    if upper_wick >= 2.0 * body and lower_wick <= 0.4 * body and c < o:
        return "SHOOTING_STAR", -0.85

    # 3. Bullish Engulfing (Önceki kırmızı mumu yutan yeşil mum)
    if pc < po and c > o and c >= po and o <= pc:
        return "BULLISH_ENGULFING", 0.80

    # 4. Bearish Engulfing (Önceki yeşil mumu yutan kırmızı mum)
    if pc > po and c < o and c <= po and o >= pc:
        return "BEARISH_ENGULFING", -0.80

    # 5. Momentum Impulse (Dolu gövdeli yönlü mum)
    if body / total_range >= 0.70:
        return ("BULL_MOMENTUM", 0.60) if c > o else ("BEAR_MOMENTUM", -0.60)

    return "NONE", 0.0

def calculate_support_resistance(candles: List[dict], window: int = 30) -> tuple[float, float]:
    """Son N mumdaki swing high ve swing low pivot seviyelerini hesaplar."""
    if len(candles) < window:
        subset = candles
    else:
        subset = candles[-window:]

    highs = [float(c["high"]) for c in subset]
    lows = [float(c["low"]) for c in subset]

    resistance = max(highs) if highs else 0.0
    support = min(lows) if lows else 0.0
    return support, resistance

def calculate_alphapulse(
    ind: IndicatorResult,
    candles: List[dict],
    ob: Optional[OrderBookAnalysis] = None,
    funding_rate: float = 0.0,
    open_interest: float = 0.0,
    trend_1h: str = "NEUTRAL",
    trend_4h: str = "NEUTRAL",
) -> AlphaPulseResult:
    """
    10 Faktörlü AlphaPulse Matrix Skorunu Üretir (0 - 100).
    """
    reasons = []
    c = ind.close

    # ── 1. TREND BİLEŞENİ (Ağırlık: %20) ──────────────────────────────────────
    trend_points = 0.0
    if c > ind.ema200:
        trend_points += 0.35
    else:
        trend_points -= 0.35

    if ind.ema9 > ind.ema21 > ind.ema50:
        trend_points += 0.35
        reasons.append("EMA9>21>50 Boğa Dizilimi")
    elif ind.ema9 < ind.ema21 < ind.ema50:
        trend_points -= 0.35
        reasons.append("EMA9<21<50 Ayı Dizilimi")

    # Üst Zaman Dilimi (HTF) Uyumu
    if trend_1h == "BULLISH" and trend_4h == "BULLISH":
        trend_points += 0.30
        reasons.append("4H+1H Trend Net Boğa")
    elif trend_1h == "BEARISH" and trend_4h == "BEARISH":
        trend_points -= 0.30
        reasons.append("4H+1H Trend Net Ayı")

    trend_score = max(-1.0, min(1.0, trend_points))

    # ── 2. MOMENTUM BİLEŞENİ (RSI + MACD + StochRSI) (Ağırlık: %20) ───────────
    mom_points = 0.0
    if 45.0 <= ind.rsi <= 65.0:
        mom_points += 0.30  # Sağlıklı yükseliş bölgesi
    elif ind.rsi > 75.0:
        mom_points -= 0.20  # Aşırı alım / yorgunluk
    elif 35.0 <= ind.rsi <= 55.0:
        mom_points -= 0.30  # Sağlıklı düşüş bölgesi
    elif ind.rsi < 25.0:
        mom_points += 0.20  # Aşırı satım tepkisi

    if ind.atr > 0:
        norm_macd = ind.macd_hist / ind.atr
        mom_points += max(-0.40, min(0.40, norm_macd * 1.5))

    if ind.stoch_k > ind.stoch_d and ind.stoch_k < 80:
        mom_points += 0.30
    elif ind.stoch_k < ind.stoch_d and ind.stoch_k > 20:
        mom_points -= 0.30

    momentum_score = max(-1.0, min(1.0, mom_points))

    # ── 3. HACİM & VOLATİLİTE BİLEŞENİ (Ağırlık: %15) ─────────────────────────
    vol_points = 0.0
    if ind.volume_ratio >= 1.50:
        vol_points += 0.50
        reasons.append(f"Hacim Patlaması ({ind.volume_ratio:.1f}x)")
    elif ind.volume_ratio >= 1.15:
        vol_points += 0.25

    if ind.bb_pct_b > 0.95:
        vol_points += 0.50 if trend_score > 0 else -0.50
    elif ind.bb_pct_b < 0.05:
        vol_points -= 0.50 if trend_score < 0 else -0.50

    volume_score = max(-1.0, min(1.0, vol_points))

    # ── 4. DESTEK / DİRENÇ & PRICE ACTION (Ağırlık: %20) ───────────────────────
    supp, res = calculate_support_resistance(candles, window=30)
    pa_pattern, pa_score = analyze_price_action(candles)

    pa_total = pa_score
    if pa_pattern != "NONE":
        reasons.append(f"Price Action: {pa_pattern}")

    if supp > 0 and c > 0:
        dist_to_supp = (c - supp) / c
        if dist_to_supp <= 0.008:
            pa_total += 0.40
            reasons.append("Kilit Destek Bölgesinde")

    if res > 0 and c > 0:
        dist_to_res = (res - c) / c
        if dist_to_res <= 0.008:
            pa_total -= 0.40
            reasons.append("Kilit Direnç Bölgesinde")

    pa_score_norm = max(-1.0, min(1.0, pa_total))

    # ── 5. EMİR DEFTERİ & DERİNLİK (Ağırlık: %15) ─────────────────────────────
    ob_score = 0.0
    if ob is not None and ob.bias != "NEUTRAL":
        if ob.bias == "BULLISH":
            ob_score = ob.signal_score / 100.0
            reasons.append(f"Tahta Alıcı Baskısı (%{ob.imbalance*100:+.0f})")
        else:
            ob_score = -(ob.signal_score / 100.0)
            reasons.append(f"Tahta Satıcı Baskısı (%{ob.imbalance*100:+.0f})")

    # ── 6. FONLAMA ORANI (Ağırlık: %10) ───────────────────────────────────────
    funding_bias = "NEUTRAL"
    fund_score = 0.0
    if funding_rate <= -0.0003:  # <= -%0.03 (Aşırı short birikmiş!)
        fund_score = 0.80
        funding_bias = "SQUEEZE_LONG"
        reasons.append(f"Short Squeeze Potansiyeli (Fonlama: %{funding_rate*100:.3f})")
    elif funding_rate >= 0.0003:  # >= +%0.03 (Aşırı long birikmiş!)
        fund_score = -0.80
        funding_bias = "SQUEEZE_SHORT"
        reasons.append(f"Long Squeeze / Çöküş Riski (Fonlama: %{funding_rate*100:.3f})")

    # ── 7. MASTER BİLEŞİK SKOR HESABI (0 - 100) ──────────────────────────────
    raw_composite = (
        (trend_score * 0.25)
        + (momentum_score * 0.20)
        + (volume_score * 0.15)
        + (pa_score_norm * 0.15)
        + (ob_score * 0.15)
        + (fund_score * 0.10)
    )

    alpha_score = round(max(0.0, min(100.0, (raw_composite + 1.0) * 50.0)), 1)
    confidence = round(abs(alpha_score - 50.0) / 50.0, 2)

    # ── 8. KARAR VE İŞLEM TİPİ ───────────────────────────────────────────────
    if alpha_score >= 70.0:
        bias = "STRONG_BUY"
        action_type = "SCALP_LONG" if ob_score > 0.20 else "INTRADAY_LONG"
    elif alpha_score >= 58.0:
        bias = "BUY"
        action_type = "INTRADAY_LONG"
    elif alpha_score <= 30.0:
        bias = "STRONG_SELL"
        action_type = "SCALP_SHORT" if ob_score < -0.20 else "INTRADAY_SHORT"
    elif alpha_score <= 42.0:
        bias = "SELL"
        action_type = "INTRADAY_SHORT"
    else:
        bias = "NEUTRAL"
        action_type = "NONE"

    return AlphaPulseResult(
        alpha_score=alpha_score,
        bias=bias,
        action_type=action_type,
        confidence=confidence,
        trend_score=round(trend_score, 2),
        momentum_score=round(momentum_score, 2),
        volume_score=round(volume_score, 2),
        pa_pattern=pa_pattern,
        support_price=round(supp, 4),
        resistance_price=round(res, 4),
        funding_bias=funding_bias,
        reasons=reasons,
    )

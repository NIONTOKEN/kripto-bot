"""
ML sinyal güçlendirici.
RandomForestClassifier kullanarak fiyat yönü olasılığı tahmin eder.
Model, başlangıçta geçmiş mum verisinden eğitilir ve periyodik olarak yenilenir.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

import ta

from app.config import config
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Gelecek N mumda fiyatın yüzde kaç hareketle "up" sayılacağı
_FUTURE_BARS = 3        # 15m × 3 = 45 dakika sonrası
_UP_THRESHOLD = 0.003   # %0.3


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Ham OHLCV'den özellik matrisi oluştur."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    feat = pd.DataFrame(index=df.index)

    # RSI
    feat["rsi"] = ta.momentum.rsi(close, window=14)

    # MACD histogram (normalize edilmiş)
    macd_obj = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    feat["macd_hist"] = macd_obj.macd_diff() / close

    # Bollinger %B
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    feat["bb_pct_b"] = bb.bollinger_pband()

    # EMA hizalama skoru: kaç EMA hizalı (0-4 arası, normalize 0-1)
    ema9 = ta.trend.ema_indicator(close, window=9)
    ema21 = ta.trend.ema_indicator(close, window=21)
    ema50 = ta.trend.ema_indicator(close, window=50)
    ema200 = ta.trend.ema_indicator(close, window=200)
    feat["ema_align"] = (
        (ema9 > ema21).astype(int)
        + (ema21 > ema50).astype(int)
        + (ema50 > ema200).astype(int)
    ) / 3.0 * 2.0 - 1.0  # -1 to +1

    # Hacim oranı
    vol_sma = volume.rolling(20).mean()
    feat["vol_ratio"] = volume / vol_sma.clip(lower=1e-9)

    # ATR%
    atr = ta.volatility.average_true_range(high, low, close, window=14)
    feat["atr_pct"] = atr / close

    # ADX
    adx_obj = ta.trend.ADXIndicator(high, low, close, window=14)
    feat["adx"] = adx_obj.adx() / 100.0
    feat["adx_dir"] = (adx_obj.adx_pos() - adx_obj.adx_neg()) / 100.0

    # Stochastic RSI
    stoch = ta.momentum.StochRSIIndicator(close, window=14)
    feat["stoch_k"] = stoch.stochrsi_k()

    # Price vs EMA21
    feat["price_vs_ema21"] = (close - ema21) / ema21

    return feat


class MLModel:
    """Sembol başına RandomForest modeli eğitir ve sinyal olasılığı üretir."""

    def __init__(self) -> None:
        # symbol → (model, scaler, last_train_time)
        self._models: Dict[str, Tuple[RandomForestClassifier, StandardScaler, float]] = {}
        self._training_lock = asyncio.Lock()

    def _make_labels(self, close_arr: np.ndarray) -> np.ndarray:
        """Her bar için N-bar sonraki yüzde değişimini hesapla ve etiketle."""
        n = _FUTURE_BARS
        labels = np.zeros(len(close_arr), dtype=int)
        for i in range(len(close_arr) - n):
            ret = (close_arr[i + n] - close_arr[i]) / close_arr[i]
            labels[i] = 1 if ret > _UP_THRESHOLD else 0
        return labels

    def _train(
        self, candles: List[dict]
    ) -> Optional[Tuple[RandomForestClassifier, StandardScaler]]:
        if len(candles) < config.ML_MIN_SAMPLES + _FUTURE_BARS + 50:
            return None

        df = pd.DataFrame(candles)
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)

        feat_df = _build_features(df)
        labels = self._make_labels(df["close"].values)

        # Son N-bar'ı dışla (etiket yok)
        valid_idx = len(candles) - _FUTURE_BARS
        feat_df = feat_df.iloc[:valid_idx]
        labels = labels[:valid_idx]

        # NaN temizle
        mask = feat_df.notna().all(axis=1)
        feat_df = feat_df[mask]
        labels = labels[mask]

        if len(feat_df) < config.ML_MIN_SAMPLES:
            return None

        X = feat_df.values
        y = labels

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=6,
            min_samples_leaf=10,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_scaled, y)
        return model, scaler

    async def train_symbol(self, symbol: str, candles: List[dict]) -> bool:
        """Bir sembol için modeli (yeniden) eğit. Thread-safe."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._train, candles)
        if result is None:
            logger.debug(f"ML [{symbol}]: yetersiz veri, model eğitilmedi")
            return False
        model, scaler = result
        self._models[symbol] = (model, scaler, time.time())
        logger.info(f"ML [{symbol}]: model eğitildi ({len(candles)} mum)")
        return True

    def needs_retrain(self, symbol: str) -> bool:
        if symbol not in self._models:
            return True
        _, _, last_train = self._models[symbol]
        elapsed_hours = (time.time() - last_train) / 3600
        return elapsed_hours >= config.ML_RETRAIN_HOURS

    def predict_long_probability(self, symbol: str, candles: List[dict]) -> float:
        """
        LONG yönü olasılığını döner (0.0-1.0).
        Model yoksa 0.5 (belirsiz) döner.
        """
        if symbol not in self._models or len(candles) < 50:
            return 0.5

        model, scaler, _ = self._models[symbol]
        df = pd.DataFrame(candles[-100:])
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)

        try:
            feat_df = _build_features(df)
            last_feat = feat_df.iloc[[-1]]
            if last_feat.isna().any(axis=1).item():
                return 0.5
            X = scaler.transform(last_feat.values)
            prob = model.predict_proba(X)[0]
            # prob[1] = UP olasılığı
            classes = list(model.classes_)
            if 1 in classes:
                return float(prob[classes.index(1)])
            return 0.5
        except Exception as exc:
            logger.warning(f"ML tahmin hatası [{symbol}]: {exc}")
            return 0.5

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import Trade
from app.strategy.indicators import IndicatorResult
from app.strategy.regime import Regime
from app.strategy.signals import SignalResult


class DummyClient:
    def get_min_qty(self, symbol: str) -> Decimal:
        return Decimal('1.0')

    def get_min_notional(self, symbol: str) -> Decimal:
        return Decimal('5.0')

    def get_step_size(self, symbol: str) -> Decimal:
        return Decimal('1.0')

    def round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        return Decimal(str(int(qty)))

    def round_price(self, symbol: str, price: Decimal) -> Decimal:
        return Decimal(f'{price:.4f}')


def make_indicators(close: float, atr: float) -> IndicatorResult:
    return IndicatorResult(
        close=close,
        high=close * 1.01,
        low=close * 0.99,
        volume=1000.0,
        ema9=close,
        ema21=close,
        ema50=close,
        ema200=close,
        rsi=50.0,
        macd=0.0,
        macd_signal=0.0,
        macd_hist=0.0,
        bb_upper=close * 1.02,
        bb_middle=close,
        bb_lower=close * 0.98,
        bb_pct_b=0.5,
        atr=atr,
        atr_pct=atr / close,
        adx=25.0,
        adx_pos=20.0,
        adx_neg=20.0,
        stoch_k=50.0,
        stoch_d=50.0,
        vwap=close,
        price_vs_vwap=0.0,
        volume_sma20=1000.0,
        volume_ratio=1.0,
        valid=True,
    )


class TestTradeLifecycle(unittest.TestCase):
    def test_trade_lifecycle_and_duration(self):
        now = datetime.now(timezone.utc)
        opened = now - timedelta(minutes=5)
        closed = now

        trade = Trade(
            id=1,
            symbol='BTCUSDT',
            side='LONG',
            entry_price=Decimal('60000'),
            sl_price=Decimal('59000'),
            tp_price=Decimal('62000'),
            quantity=Decimal('0.01'),
            leverage=15,
            status='OPEN',
            opened_at=opened,
        )
        self.assertEqual(trade.status, 'OPEN')
        self.assertIsNone(trade.closed_at)

        trade.status = 'CLOSED'
        trade.closed_at = closed
        trade.close_reason = 'QUICK_TP'
        trade.pnl = Decimal('1.50')
        trade.pnl_pct = Decimal('2.5')

        self.assertEqual(trade.status, 'CLOSED')
        self.assertIsNotNone(trade.closed_at)
        duration_seconds = (trade.closed_at - trade.opened_at).total_seconds()
        self.assertGreater(duration_seconds, 0)
        self.assertAlmostEqual(duration_seconds, 300, delta=10)


class TestDynamicLeverageAndRisk(unittest.TestCase):
    def setUp(self):
        from app.risk.manager import RiskManager
        self.dummy_client = DummyClient()
        self.risk_manager = RiskManager(self.dummy_client)

    def test_dynamic_leverage_scalp(self):
        ind = make_indicators(close=1.0, atr=0.015)
        balance = Decimal('10.0')

        qty, sl_price, tp_price, leverage = self.risk_manager.calculate_position_size(
            symbol='TESTUSDT',
            ind=ind,
            balance=balance,
            direction='LONG',
            signal_type='SCALP_LONG',
        )
        self.assertIsNotNone(qty)
        self.assertIsNotNone(sl_price)
        self.assertIsNotNone(tp_price)
        self.assertGreaterEqual(leverage, 15)
        self.assertGreaterEqual(qty * Decimal('1.0'), Decimal('5.0'))
        self.assertLess(sl_price, Decimal('1.0'))
        self.assertGreater(tp_price, Decimal('1.0'))

    def test_dynamic_leverage_macro(self):
        ind = make_indicators(close=2.0, atr=0.05)
        balance = Decimal('20.0')

        qty, sl_price, tp_price, leverage = self.risk_manager.calculate_position_size(
            symbol='TESTUSDT',
            ind=ind,
            balance=balance,
            direction='SHORT',
            signal_type='COLLAPSE_SHORT',
        )
        self.assertIsNotNone(qty)
        self.assertLessEqual(leverage, 8)
        self.assertGreater(sl_price, Decimal('2.0'))
        self.assertLess(tp_price, Decimal('2.0'))


if __name__ == '__main__':
    unittest.main()

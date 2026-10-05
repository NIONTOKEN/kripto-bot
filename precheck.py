import asyncio, sys
sys.path.insert(0, '.')
from app.exchange.client import BinanceClient
from app.config import config

async def pre_check():
    mod = "TESTNET" if config.BINANCE_TESTNET else "MAINNET"
    print("=== ON KONTROL ===")
    print(f"Mod: {mod}")
    print(f"Leverage: {config.LEVERAGE}x")
    print(f"Max Pozisyon: {config.MAX_OPEN_POSITIONS}")
    print(f"Min Skor: {config.MIN_SCORE_TO_OPEN}")
    print()
    c = BinanceClient()
    await c.start()
    try:
        bal = await c.get_balance_usdt()
        wallet = await c.get_wallet_balance_usdt()
        total = await c.get_total_balance_usdt()
        positions = await c.get_positions()
        print(f"Serbest bakiye:  {bal:.4f} USDT")
        print(f"Cuzdan bakiye:   {wallet:.4f} USDT")
        print(f"Toplam deger:    {total:.4f} USDT")
        print(f"Acik pozisyon:   {len(positions)} adet")
        for p in positions:
            side = "LONG" if float(p["positionAmt"]) > 0 else "SHORT"
            print(f"  -> {p['symbol']} {side} {p['positionAmt']} @ {p['entryPrice']} | PnL: {p['unRealizedProfit']} USDT")
        print()
        print("=== BAGLANTI TAMAM - BOT CALISTIRMAYA HAZIR ===")
    except Exception as e:
        print(f"HATA: {e}")
    await c.stop()

asyncio.run(pre_check())

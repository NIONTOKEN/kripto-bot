# 🤖 Kripto Futures Trading Bot

Binance USDT Perpetual Futures için tam otomatik kaldıraçlı trading botu.
Bilgisayar kapalıyken bile **Telegram** + **Web dashboard** üzerinden takip edilebilir.

---

## ⚙️ Kurulum

### Gereksinimler
- Python 3.11+
- Binance Futures hesabı (API key gerekli)

### 1. Sanal ortam oluştur
```bash
# Linux / macOS / VPS
python -m venv .venv
source .venv/bin/activate

# Windows
python -m venv .venv
.venv\Scripts\activate
```

### 2. Bağımlılıkları yükle
```bash
pip install -r requirements.txt
```

### 3. .env dosyasını oluştur
```bash
# Linux/macOS
cp .env.example .env

# Windows
copy .env.example .env
```

Ardından `.env` dosyasını bir metin editörüyle aç ve değerleri gir:
```
BINANCE_API_KEY=senin_api_anahtarın
BINANCE_SECRET_KEY=senin_gizli_anahtarın
BINANCE_TESTNET=true          ← önce true ile test et!
TELEGRAM_BOT_TOKEN=...        ← isteğe bağlı
TELEGRAM_CHAT_ID=...          ← isteğe bağlı
```

### 4. Botu çalıştır
```bash
python -m app.main
```

### 5. Dashboard'u aç
```
http://127.0.0.1:8000
```

---

## 🛰️ Bilgisayar Kapalıyken Takip (VPS Kurulumu)

Bot bir **VPS** (sanal sunucu) üzerinde çalıştırıldığında bilgisayarın kapalı olması önemli değildir.

### DigitalOcean / AWS / Hetzner VPS Kurulumu

```bash
# 1. Botu VPS'e kopyala
scp -r "kripto ticareti" user@sunucu_ip:/home/user/

# 2. VPS'e bağlan
ssh user@sunucu_ip

# 3. Python ve bağımlılıkları kur
cd "kripto ticareti"
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. .env oluştur
cp .env.example .env && nano .env
```

### Botu arka planda çalıştır (systemd)

```ini
# /etc/systemd/system/kripto-bot.service
[Unit]
Description=Kripto Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kripto ticareti
ExecStart=/home/ubuntu/kripto ticareti/.venv/bin/python -m app.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable kripto-bot
sudo systemctl start kripto-bot
sudo systemctl status kripto-bot

# Logları izle
sudo journalctl -u kripto-bot -f
```

### Dashboard'a uzaktan eriş

`.env` içinde `DASHBOARD_HOST=0.0.0.0` ve `DASHBOARD_PORT=8000` olduğundan emin ol.

Tarayıcıdan: `http://sunucu_ip:8000`

> **Güvenlik notu:** Dashboard'u dışarıya açmadan önce VPS'in firewall'ında sadece kendi IP'ne izin ver:
> ```bash
> ufw allow from SENIN_IP to any port 8000
> ```

---

## 📊 Strateji

### Sinyal Bileşenleri

| Bileşen | Ağırlık | Açıklama |
|---------|---------|----------|
| EMA Hizalaması | %25 | 9/21/50/200 EMA sıralaması |
| RSI | %15 | Aşırı alım/satım tespiti |
| MACD | %20 | Histogram yönü ve büyüklüğü |
| Bollinger Bands | %15 | %B konumu |
| Hacim | %10 | Hacim × SMA20 oranı |
| ADX | %15 | Trend gücü ve yönü |

### ML Modeli
- **Algoritma:** RandomForestClassifier (scikit-learn)
- **Özellikler:** RSI, MACD hist, BB %B, EMA hizalaması, hacim oranı, ATR%, ADX, StochRSI
- **Etiket:** Sonraki 3 mumda fiyat >%0.3 yukarı çıktı mı?
- **Yeniden eğitim:** Her 4 saatte bir

### Piyasa Rejimleri
- `BULL_TREND` (EMA hizalı + ADX>25 + DI+ > DI-): %100 sinyal
- `BEAR_TREND` (EMA ters hizalı + ADX>25 + DI- > DI+): %100 sinyal
- `RANGING` (ADX<20): %70 sinyal
- `VOLATILE` (ATR>%3): %50 sinyal

### Pozisyon Boyutlandırma
```
Risk miktarı = Bakiye × RISK_PER_TRADE_PCT / 100
SL mesafesi  = ATR × SL_ATR_MULTIPLIER
Miktar       = Risk miktarı / SL mesafesi
```

---

## 🛡️ Güvenlik Kuralları

| Kural | Durum |
|-------|-------|
| Aynı sembolde çift pozisyon yok | ✅ |
| Zarar eden pozisyon büyütme yok | ✅ |
| Martingale yok | ✅ |
| SL asla kaldırılmaz | ✅ |
| Doğrulanamayan bakiyeyle işlem yok | ✅ |
| Stale data (>120sn) ile işlem yok | ✅ |
| Min notional altında işlem yok | ✅ |
| Günlük kayıp limiti | ✅ |
| Maksimum kaldıraç sınırı | ✅ |

---

## 📁 Dosya Yapısı

```
app/
├── main.py                  # Giriş noktası
├── config.py                # .env yapılandırması
├── database.py              # SQLite veritabanı
├── bot.py                   # Ana orkestratör
├── exchange/
│   ├── client.py            # Binance REST API
│   └── websocket.py         # WebSocket yöneticisi
├── strategy/
│   ├── indicators.py        # Teknik indikatörler
│   ├── ml_model.py          # ML modeli
│   ├── regime.py            # Piyasa rejimi
│   └── signals.py           # Sinyal skoru
├── risk/
│   └── manager.py           # Risk yönetimi
├── execution/
│   └── order_manager.py     # Emir yönetimi
├── notifications/
│   └── telegram.py          # Telegram bildirimleri
└── dashboard/
    ├── server.py             # FastAPI sunucu
    └── templates/index.html # Dashboard UI
```

---

## 📝 Log Örnekleri

```
2026-09-10 12:00:00 INFO  [bot      ] Bot hazır — 20 sembol takip ediliyor
2026-09-10 12:00:01 INFO  [signals  ] SIGNAL BTCUSDT LONG score=82.3 confidence=0.81 regime=BULL_TREND
2026-09-10 12:00:01 INFO  [order_ma ] ORDER BTCUSDT LONG qty=0.002 fill=65432.10 order_id=12345
2026-09-10 12:00:01 INFO  [order_ma ] PROTECTION BTCUSDT SL=64800.00 order_id=12346
2026-09-10 12:00:01 INFO  [order_ma ] PROTECTION BTCUSDT TP=67200.00 order_id=12347
2026-09-10 12:15:30 INFO  [order_ma ] CLOSE BTCUSDT reason=TP pnl=+12.4500 USDT (+8.24%)
```

---

## ⚠️ Risk Uyarısı

> Bu bot **gerçek para** ile işlem yapabilir. Kripto para piyasaları son derece volatildir.
> **Kaybetmeyi göze alabileceğinden fazlasını riske atma.**
> İlk olarak mutlaka `BINANCE_TESTNET=true` ile test et.

---

## 🔧 Sorun Giderme

**"Zorunlu environment değişkeni ayarlanmamış" hatası:**
→ `.env` dosyasının mevcut dizinde olduğundan emin ol.

**"Bakiye çok düşük" hatası:**
→ `.env` içinde `MIN_BALANCE_USDT` değerini düşür veya bakiye yükle.

**WebSocket bağlantı hatası:**
→ `BINANCE_TESTNET=true` için testnet URL'si kullanıldığını kontrol et.

**Dashboard'a erişilemiyor:**
→ `DASHBOARD_HOST=0.0.0.0` ayarlandığından ve port'un firewall'da açık olduğundan emin ol.

# FuturesFinder5000

Real-time crypto and leveraged ETF trading system powered by **Google Gemini 3.5 Flash**.
Autonomous agents handle monitoring, sentiment, backtesting, and end-of-day cleanup so the
LLM can focus on signal generation.

---

## Architecture

```
Coinbase WebSocket --> crypto     --|
Alpaca WebSocket   --> ingest     --|
RSS / NewsAPI      --> digest     --|--> PostgreSQL --> analyze (Gemini 3.5 Flash) --> executor
                                    |                                                      |
Resource / health  --> watchdog   --|                                          suggest / paper / live
Trade settlement   --> settler    --|
                                          Flask API --> Web Dashboard (port 5001)
```

### Services

| Container     | Role                                                          |
|---------------|---------------------------------------------------------------|
| `ff_db`       | PostgreSQL - all bars, signals, trades, knowledge, inbox      |
| `ff_api`      | Flask dashboard + REST API                                    |
| `ff_crypto`   | Coinbase WebSocket feed + Gemini signal analysis (24/7)       |
| `ff_digest`   | RSS news ingestion + Agent 2 sentiment scoring                |
| `ff_settler`  | EOD settlement, outcome tagging, Agent 4 execution monitor    |
| `ff_watchdog` | Resource monitor, container liveness, Agent 1 data integrity  |
| `ff_ingest`   | Alpaca stock/ETF feed (stopped by default - market hours only)|

---

## Autonomous Agents

Seven lightweight agents run as threads or scheduled functions inside existing services -
no extra containers or processes required.

| # | Agent | Service | Frequency |
|---|-------|---------|-----------|
| 1 | **Data Integrity Monitor** | watchdog | Every ~15 min |
| 2 | **News Sentiment Analyzer** | digest | Per article ingested |
| 3 | **Backtester** | API (on-demand) | `POST /api/backtest/run` |
| 4 | **Trade Execution Monitor** | settler | Every 15 min (background thread) |
| 5 | **EOD Cleanup & Summary** | settler | Market close |
| 6 | **Crypto Monitor & Alerts** | crypto analyze | Per analysis cycle |
| 7 | **Performance Analytics** | API (on-demand) | `GET /api/analytics` |

### AI Inbox

The system has a persistent inbox so agents can leave messages between stateless sessions.
Alerts for low win rate, low balance, data gaps, execution failures, and EOD summaries are
automatically written to the inbox and surfaced in the dashboard (bell icon).

- `GET /api/inbox` - all messages
- `POST /api/inbox/mark-read` - mark as read
- `POST /api/inbox/clear` - clear all messages

---

## Leveraged ETF Pairs (Stocks - market hours only)

| Bull | Bear | Underlying | Leverage |
|------|------|------------|----------|
| TQQQ | SQQQ | QQQ        | 3x       |
| UPRO | SPXU | SPY        | 3x       |
| SOXL | SOXS | SOXX       | 3x       |

The underlying index is ingested alongside each ETF to give the LLM directional context.

## Crypto Pairs (24/7)

BTC/USD, ETH/USD, SOL/USD - live via Coinbase Advanced Trade WebSocket.
Crypto analysis runs independently of the stock service; the LLM only sees crypto context
when on the crypto page and stock context when on the stocks page.

---

## Trade Modes

| Mode    | What happens                                       |
|---------|----------------------------------------------------|
| suggest | Signal logged to DB + UI only - no orders placed   |
| paper   | Orders submitted to Alpaca paper account (free)    |
| live    | Real orders with hard position and loss caps       |

**Start in `suggest` mode**, review signals for a few days, then promote to `paper`.

---

## Day Trading Rules (enforced in code)

- No entries in first 15 minutes (9:30-9:45 ET)
- All positions force-closed at **3:45 PM ET** - leveraged ETF decay makes overnight holds toxic
- Hard daily loss halt: closes everything if daily loss exceeds `MAX_DAILY_LOSS_USD`
- ATR-based position sizing (not flat dollar) to account for leveraged volatility

## PDT Rule

The SEC eliminated the pattern day trader (PDT) $25,000 minimum equity requirement effective
**June 4, 2026**. Brokerages have until **October 20, 2027** to comply. Verify your broker's
current intraday margin policy before enabling live trading.

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/apoll024/futuresfinder5000.git
cd futuresfinder5000
cp .env.example .env
```

Edit `.env` and fill in:

```env
# Alpaca (stocks)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...

# Coinbase (crypto)
COINBASE_API_KEY=...
COINBASE_API_SECRET=...

# Google Gemini LLM
GEMINI_API_KEY=...

# Trade mode: suggest | paper | live
TRADE_MODE=suggest
```

### 2. Start services

```bash
docker compose up -d db api crypto digest watchdog settler
# ff_ingest (stocks) is stopped by default - start it during market hours if needed
```

### 3. Open the dashboard

```
http://<your-server>:5001
```

### 4. Check logs

```bash
docker compose logs -f api      # Flask dashboard
docker compose logs -f crypto   # Crypto signals
docker compose logs -f watchdog # Health + agent alerts
```

### 5. Promote to paper trading

When suggestions look good for several days, set `TRADE_MODE=paper` in `.env` and restart:

```bash
docker compose up -d
```

---

## Deployment Workflow

GitHub is the single source of truth. The VM should never have local uncommitted changes.

```
Edit locally
      |
      v
git commit + git push
      |
      v
SSH to VM -> git pull --ff-only -> docker compose build -> docker compose up -d
```

Never edit files directly on the VM.

---

## Requirements

- Docker + Docker Compose
- Alpaca Markets account - https://alpaca.markets (free)
- Coinbase Advanced Trade account - https://www.coinbase.com
- Google Gemini API key - https://aistudio.google.com
- Oracle Cloud Free Tier or any Linux server (4 OCPU / 24 GB RAM recommended)

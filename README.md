# FuturesFinder5000

Real-time leveraged ETF day trading system powered by a self-hosted LLM (Ollama).
No cloud API fees. No token dependencies. Fully yours.

## Architecture

```
Alpaca WebSocket → ingest → PostgreSQL → analyze (Ollama LLM) → executor
                                                                     ↓
                                                          suggest / paper / live
```

## Leveraged ETF Pairs

| Bull | Bear | Underlying | Leverage |
|------|------|------------|----------|
| TQQQ | SQQQ | QQQ        | 3x       |
| UPRO | SPXU | SPY        | 3x       |
| SOXL | SOXS | SOXX       | 3x       |

The system ingests the **underlying index** (QQQ, SPY, SOXX) alongside each ETF to give the LLM directional context before signaling.

## Trade Modes

| Mode    | What happens                                      |
|---------|---------------------------------------------------|
| suggest | Signal logged to DB + UI only — no orders placed  |
| paper   | Orders submitted to Alpaca paper account (free)   |
| live    | Real orders with hard position and loss caps      |

**Start in `suggest` mode**, review signals for a few days, then move to `paper`.

## Day Trading Rules (enforced in code)

- No entries in first 15 minutes (9:30–9:45 ET)
- All positions force-closed at **3:45 PM ET** — leveraged ETF decay makes overnight holds toxic
- Hard daily loss halt: closes everything if daily loss hits `MAX_DAILY_LOSS_USD`
- ATR-based position sizing (not flat dollar) to account for leveraged volatility

## PDT Rule

The SEC eliminated the pattern day trader (PDT) $25,000 minimum equity requirement effective **June 4, 2026**. Brokerages have until **October 20, 2027** to comply. Verify your broker's current intraday margin policy before enabling live trading.

## Setup

### 1. Copy and configure env
```bash
cp .env.example .env
# Add your Alpaca API keys
```

### 2. Start services
```bash
docker compose up -d
```

### 3. Pull LLM model (first run only)
```bash
docker exec ff_ollama ollama pull llama3.1:8b
```

### 4. Check logs
```bash
docker compose logs -f ingest   # real-time bar feed
docker compose logs -f api      # web dashboard
```

### 5. Upgrade to paper trading
When suggestions look good, set `TRADE_MODE=paper` in `.env` and restart.

## Requirements
- Docker + Docker Compose
- Alpaca Markets account (free at alpaca.markets)
- Oracle Cloud Free Tier or any Linux server (4 OCPU / 24 GB recommended)

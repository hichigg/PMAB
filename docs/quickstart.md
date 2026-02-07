# Quickstart Guide

Get the Polymarket arbitrage bot running in under 10 minutes.

## 1. Install

```bash
git clone <repo-url>
cd polymarket-arb
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"
```

## 2. Configure Credentials

```bash
cp config/settings.example.yaml config/settings.yaml
cp .env.example .env
```

Edit `.env` with your Polymarket API credentials:

```
POLYMARKET_API_KEY=your_key_here
POLYMARKET_API_SECRET=your_secret_here
POLYMARKET_API_PASSPHRASE=your_passphrase_here
POLYMARKET_PRIVATE_KEY=your_polygon_private_key
```

If you don't have CLOB API credentials yet:

```python
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key="your-polygon-private-key",
)
print(client.create_or_derive_api_creds())
```

## 3. Enable a Feed

Edit `config/settings.yaml` — enable at least one data feed:

```yaml
feeds:
  economic:
    enabled: true    # BLS data (CPI, NFP, etc.)
  sports:
    enabled: false   # ESPN scores
  crypto:
    enabled: false   # Binance/Coinbase/Kraken
```

## 4. Scan Markets

Verify connectivity and see what's available:

```bash
python scripts/market_scanner.py --top 10
```

## 5. Run Tests

```bash
python -m pytest tests/ -q
```

All 861+ tests should pass.

## 6. Start the Bot

```bash
python scripts/run.py
```

The bot will:
1. Connect to Polymarket CLOB API
2. Start the market scanner (discovers opportunities every 60s)
3. Start enabled data feeds (polls for new events)
4. Match events to markets and execute trades when edge is detected
5. Log all decisions as structured JSON

## 7. Monitor

### Terminal Dashboard

```bash
python scripts/dashboard.py
```

Shows: win rate, P&L, latency, category stats.

### Alerts

Enable Telegram or Discord in `config/settings.yaml` for real-time notifications on trades, kill switch triggers, and daily P&L summaries.

## Common Commands

```bash
# Run with debug logging
python scripts/run.py --log-level DEBUG

# Scan specific category
python scripts/market_scanner.py --category ECONOMIC

# Run backtest scenario
python scripts/backtest.py scenario.json --slippage-bps 10

# Docker
docker-compose up -d
docker-compose logs -f bot
```

## What Happens Next

1. **Smoke test** (1-3 events): Run with $20-50 per trade on real data releases
2. **Ramp up** (2 weeks): Scale to $200, add sports markets
3. **Full deployment**: Scale to $2,000-5,000+ across all categories

See [deployment.md](deployment.md) for production setup and [configuration.md](configuration.md) for all settings.

# Deployment Guide

## Prerequisites

- Python 3.11+
- A Polygon wallet (EOA) funded with USDC
- Polymarket CLOB API credentials
- VPS with low-latency networking (recommended: AWS US-East-1)

## Local Development

```bash
# Clone and install
git clone <repo-url>
cd polymarket-arb
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"

# Configure
cp config/settings.example.yaml config/settings.yaml
cp .env.example .env
# Edit both files with your credentials

# Run tests
python -m pytest tests/ -q

# Start the bot
python scripts/run.py
```

## Docker Deployment

```bash
# Build
docker build -t polymarket-arb .

# Configure
cp .env.example .env
# Edit .env with your credentials
# Edit config/settings.yaml with your settings

# Run
docker-compose up -d

# View logs
docker-compose logs -f bot

# Stop
docker-compose down
```

## VPS Deployment (systemd)

### 1. Provision Server

Recommended: AWS EC2 in **us-east-1** (Virginia) — closest to Polygon validators and US economic data sources.

Minimum spec: 2 vCPU, 4GB RAM, SSD.

### 2. Install Dependencies

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git

# Create bot user
sudo useradd --create-home --shell /bin/bash botuser
```

### 3. Deploy Code

```bash
sudo mkdir -p /opt/polymarket-arb
sudo chown botuser:botuser /opt/polymarket-arb
sudo -u botuser git clone <repo-url> /opt/polymarket-arb

cd /opt/polymarket-arb
sudo -u botuser python3.11 -m venv .venv
sudo -u botuser .venv/bin/pip install -e .
```

### 4. Configure

```bash
sudo -u botuser cp config/settings.example.yaml config/settings.yaml
sudo -u botuser cp .env.example .env
# Edit files: sudo -u botuser nano config/settings.yaml
# Edit files: sudo -u botuser nano .env
chmod 600 config/settings.yaml .env
```

### 5. Install systemd Service

```bash
sudo cp deploy/polymarket-arb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polymarket-arb
sudo systemctl start polymarket-arb
```

### 6. Monitor

```bash
# Status
sudo systemctl status polymarket-arb

# Logs (live)
sudo journalctl -u polymarket-arb -f

# Logs (last 100 lines)
sudo journalctl -u polymarket-arb -n 100

# Restart
sudo systemctl restart polymarket-arb
```

## Environment Variables

All sensitive values should be set in `.env` (gitignored):

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_API_KEY` | Yes | CLOB API key (HMAC-SHA256 derived) |
| `POLYMARKET_API_SECRET` | Yes | CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Yes | CLOB API passphrase |
| `POLYMARKET_PRIVATE_KEY` | Yes | Polygon EOA private key |
| `BLS_API_KEY` | No | BLS API key (optional, higher rate limits) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for alerts |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL for alerts |
| `LOG_LEVEL` | No | Log level override (default: INFO) |

## API Key Setup

### Polymarket CLOB Credentials

1. Create a Polygon wallet and fund with USDC
2. Use `py-clob-client` to derive API credentials:

```python
from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key="your-polygon-private-key",
)
creds = client.create_or_derive_api_creds()
print(creds)  # Contains api_key, api_secret, api_passphrase
```

3. Copy the output values into `.env`

### Telegram Bot

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts
2. Copy the bot token to `TELEGRAM_BOT_TOKEN`
3. Message `@userinfobot` to get your chat ID → copy to `TELEGRAM_CHAT_ID`
4. Set `alerts.telegram.enabled: true` in `config/settings.yaml`

### Discord Webhook

1. Go to your Discord channel → Settings → Integrations → Webhooks → New Webhook
2. Copy the webhook URL to `DISCORD_WEBHOOK_URL`
3. Set `alerts.discord.enabled: true` in `config/settings.yaml`

## Smoke Test Checklist

Before scaling up, verify with small trades ($20-50):

1. [ ] Bot starts without errors: `python scripts/run.py --log-level DEBUG`
2. [ ] Scanner discovers markets: check logs for `scanner_started` and `OPPORTUNITY_FOUND`
3. [ ] Feed connects: check logs for `feed_started` events
4. [ ] Market scanner CLI works: `python scripts/market_scanner.py --top 10`
5. [ ] Alert channels work: trigger a test alert and verify Telegram/Discord delivery
6. [ ] Place a manual test order through the Polymarket UI to verify wallet/USDC setup
7. [ ] Run on 1-3 real events with minimal capital
8. [ ] Verify P&L tracking: `python scripts/dashboard.py` shows correct numbers

# Polymarket Latency Arbitrage Bot — Implementation Action Plan

> **Purpose**: Step-by-step build plan for a bot that exploits the structural latency gap between real-world event outcomes becoming publicly known and Polymarket market prices/resolution catching up. Designed to be executed with Claude Code.

---

## Phase 0: Environment & Infrastructure Setup

### 0.1 — Project Scaffolding
- Initialize a Python project (3.11+) with `pyproject.toml`
- Set up the following directory structure:
  ```
  polymarket-arb/
  ├── src/
  │   ├── core/           # Config, logging, shared types
  │   ├── feeds/          # Data source connectors (econ, sports, crypto)
  │   ├── polymarket/     # CLOB client wrapper, order management
  │   ├── strategy/       # Decision engine per market category
  │   ├── risk/           # Position limits, kill switches, oracle risk
  │   └── monitor/        # Dashboard, alerts, P&L tracking
  ├── tests/
  ├── scripts/            # One-off utilities (market scanner, backtest)
  ├── config/
  │   └── settings.yaml   # API keys, thresholds, market whitelist
  └── docs/
  ```
- Install core dependencies:
  - `py-clob-client` (Polymarket official — `pip install py-clob-client`)
  - `websockets` / `aiohttp` for async data feeds
  - `web3` / `eth-account` for Polygon interaction and EIP-712 signing
  - `pydantic` for config validation
  - `structlog` for structured logging

### 0.2 — Polymarket Account & API Access
- Create a Polymarket-compatible Polygon wallet (EOA)
- Fund with USDC on Polygon (start with $500–$1,000 test capital)
- Generate L2 CLOB API credentials:
  - Derive API key via HMAC-SHA256 signing flow (see `py-clob-client` auth docs)
  - Store credentials in `config/settings.yaml` (gitignored) or environment variables
- Verify connectivity: fetch a market's order book, place and cancel a tiny limit order

### 0.3 — VPS / Server
- Provision a VPS on **AWS US-East-1 (Virginia)** — closest to Polygon validators and US data sources
- Minimum spec: 2 vCPU, 4GB RAM, SSD, low-latency networking
- Install Python, set up systemd service for the bot process
- Estimated cost: ~$60–$100/month

---

## Phase 1: Polymarket Connectivity Layer

### 1.1 — CLOB Client Wrapper (`src/polymarket/client.py`)
Build a thin async wrapper around `py-clob-client` that exposes:
- `get_markets()` — list active markets, filter by category/tag
- `get_orderbook(condition_id)` — fetch current bids/asks and depth
- `subscribe_orderbook(condition_id, callback)` — WebSocket subscription to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `place_order(side, price, size, order_type)` — supports FOK (Fill-Or-Kill) for arb execution
- `cancel_order(order_id)` / `cancel_all()`
- Rate limit awareness: 500 orders/sec burst, 60/sec sustained

### 1.2 — Order Pre-computation Module (`src/polymarket/presigner.py`)
For known binary outcomes, **pre-sign orders before the event**:
- Before a CPI release, prepare two signed FOK buy orders:
  - One for YES at a target price (e.g., buy YES at $0.85 if CPI > consensus)
  - One for NO at a target price (if CPI < consensus)
- On event trigger, submit the correct pre-signed order — shaving ~20–50ms off execution
- Implement EIP-712 structured data signing using `eth-account`

### 1.3 — Market Scanner (`scripts/market_scanner.py`)
Build a utility that:
- Polls Polymarket's Gamma API (`https://gamma-api.polymarket.com`) for all active markets
- Filters for target categories: `economics`, `fed`, `sports`, `crypto`
- Scores each market by: volume, liquidity depth at extremes (near $0 / $1), days to resolution, fee status
- Outputs a ranked list of candidate markets for the bot to target
- Run daily or on-demand

---

## Phase 2: Data Feed Connectors

### 2.1 — Economic Data Feed (`src/feeds/economic.py`)

**Priority 1 — Target markets**: Fed rate decisions (FOMC), CPI, Non-Farm Payrolls, GDP

**Implementation options (choose based on budget)**:

**Option A — Custom BLS/Fed scraper (free, ~500ms–2s latency)**:
- Monitor `https://www.bls.gov/` and `https://www.federalreserve.gov/` for page changes
- Poll every 100ms starting 5 seconds before scheduled release time
- Parse the headline number (e.g., CPI YoY %) using regex or targeted HTML parsing
- Compare to Polymarket market threshold to determine outcome

**Option B — AlphaFlash API (paid, ~50–200ms latency)**:
- Subscribe to AlphaFlash machine-readable macro data service
- Covers 400+ indicators including FOMC, CPI, NFP, GDP
- Hosted in CME Aurora / Equinix NY2 data centers
- Pricing is enterprise — reach out to sales@alphaflash.com

**Key implementation details**:
- Map each Polymarket market condition to its resolution source and threshold
- Example: "CPI YoY above 3.0% for January?" → parse BLS CPI report → compare to 3.0%
- Must handle edge cases: data revisions, delayed releases, website format changes
- Implement a confidence score: only fire if parsed value is unambiguous

### 2.2 — Sports Data Feed (`src/feeds/sports.py`)

**Priority 2 — Target markets**: NFL, NBA, NHL moneylines, spreads, over/unders

**Implementation options**:

**Option A — Sportradar Live Data (gold standard, sub-1s, $10K+/month)**:
- Push-based WebSocket feed, scores within 1 second of in-venue action
- 3–6 seconds ahead of TV broadcast
- Covers NBA, NFL, NHL, MLB, UEFA, etc.

**Option B — Free/cheap sports APIs (~5–30s latency)**:
- ESPN API, The Odds API, or similar free endpoints
- Sufficient for end-of-game scenarios where the margin is minutes, not seconds
- Good enough for initial testing and proof of concept

**Key implementation details**:
- Focus on **game-ending events**: final whistle, clock expiry, walk-off, etc.
- When final score is confirmed, determine Polymarket market outcomes (moneyline winner, over/under, spread)
- Be aware of Polymarket's **3-second marketable order delay** on sports markets
- Pre-compute all possible outcomes before game ends (team A wins, team B wins)

### 2.3 — Crypto Price Feed (`src/feeds/crypto.py`)

**Priority 3 — Only target daily/weekly markets (15-min markets have 3.15% fees)**

**Implementation**:
- Connect to Binance WebSocket: `wss://stream.binance.com:9443/ws/btcusdt@trade`
- Also connect Coinbase, Kraken for cross-validation
- For daily price target markets (e.g., "BTC above $100K at midnight UTC?"):
  - Monitor price in final minutes before resolution timestamp
  - When outcome is certain (price far enough from threshold with <1 min remaining), fire order
- Latency: 1–10ms from Binance, far faster than Polymarket reaction

---

## Phase 3: Strategy / Decision Engine

### 3.1 — Core Arbitrage Logic (`src/strategy/latency_arb.py`)

```
FOR each monitored market:
    1. WAIT for trigger event (data release, game end, price threshold)
    2. DETERMINE outcome from fastest data source
    3. CHECK confidence ≥ 99% (hard requirement)
    4. FETCH current Polymarket orderbook
    5. CALCULATE expected profit:
       - available_liquidity = sum of resting orders on losing side
       - entry_price = weighted avg fill price for our order size
       - expected_payout = $1.00 per share at resolution
       - profit = (1.00 - entry_price) × size - fees - gas
    6. IF profit > minimum_threshold ($10):
       - SUBMIT FOK buy order for winning outcome
    7. LOG result, update P&L
```

### 3.2 — Market-Specific Strategy Configs (`config/strategies/`)

Create a YAML config per market category:

```yaml
# config/strategies/fed_decisions.yaml
category: economic
subcategory: fomc_rate_decision
data_source: bls_scraper  # or alphaflash
min_confidence: 0.99
min_profit_usd: 10
max_position_usd: 5000
order_type: FOK
pre_sign_orders: true
fee_rate: 0.0  # zero fees on economic markets
market_identifiers:
  - pattern: "Fed rate"
  - pattern: "FOMC"
  - pattern: "interest rate"
```

### 3.3 — Opportunity Prioritization
When multiple markets are exploitable simultaneously (e.g., FOMC affects multiple rate markets):
- Rank by: `available_liquidity × (1.0 - entry_price)` (expected profit)
- Execute highest-value first
- Capital allocation: never risk more than 20% of bankroll on a single event

---

## Phase 4: Risk Management

### 4.1 — Kill Switches (`src/risk/kill_switch.py`)
- **Max daily loss**: If cumulative daily P&L drops below -$X, halt all trading
- **Max position size**: Cap per-market and per-event exposure
- **Oracle risk filter**: Skip markets where resolution criteria are ambiguous or subjective
  - Maintain a blacklist of market types with known resolution disputes
  - Avoid markets with phrases like "at discretion of", "as determined by", or ambiguous criteria
- **Connectivity check**: Verify Polymarket API and data feed latency before each trade

### 4.2 — Oracle Risk Mitigation (`src/risk/oracle_monitor.py`)
- Monitor UMA oracle proposals for your active markets
- If a dispute is filed on a market you hold shares in, alert immediately
- Track known UMA whale wallets (top 2 control >50% of voting power)
- Set a maximum exposure to any single UMA-resolved market
- Consider hedging: if holding YES shares at $0.98, the 2% is payment for oracle risk

### 4.3 — Market Quality Filters
Before trading any market, verify:
- Orderbook depth > $500 on the side you'd trade against
- Bid-ask spread < 10 cents (otherwise slippage eats profits)
- Market is not flagged/paused by Polymarket
- No active UMA disputes on this market
- Fee rate is 0% (reject markets with dynamic fees unless margin is huge)

---

## Phase 5: Monitoring & Operations

### 5.1 — Logging & Alerting (`src/monitor/`)
- Structured JSON logs for every decision: event detected, confidence, orderbook snapshot, order submitted, fill result
- Real-time Telegram/Discord alerts for:
  - Trade executed (with P&L)
  - Kill switch triggered
  - Data feed disconnection
  - UMA dispute filed on held position
- Daily P&L summary

### 5.2 — Performance Dashboard (`scripts/dashboard.py`)
Track and visualize:
- Win rate per market category
- Average profit per trade
- Latency histogram: time from event to order fill
- Cumulative P&L curve
- Liquidity captured vs. available

### 5.3 — Backtesting Framework (`scripts/backtest.py`)
- Record all Polymarket orderbook snapshots around target events
- Replay events with historical data feeds to estimate what the bot would have captured
- Use this to calibrate position sizes and validate strategy changes

---

## Phase 6: Deployment & Iteration

### 6.1 — Staged Rollout
This is a deterministic strategy (reacting to known outcomes, not predicting), so extended paper trading adds no value. The only risk is mechanical — bad plumbing, not bad logic.

1. **Smoke Test (1–3 events)**: Run the full pipeline live with **$20–$50 per trade** on real economic data releases (e.g., one CPI, one FOMC). Purpose: confirm data parsing, order signing, FOK execution, and latency all work end-to-end under real conditions. If all 1–3 trades execute correctly → proceed.
2. **Ramp Up (next 2 weeks)**: Scale to $200 capital max, economic data + sports markets. Validate across multiple market categories.
3. **Full Deployment (Month 2+)**: Scale to $2,000–$5,000+ capital across all validated categories, increase per-trade limits based on available liquidity.

### 6.2 — Build Priority Order
Execute phases in this order for fastest time-to-revenue:

| Priority | Task | Rationale |
|----------|------|-----------|
| P0 | Polymarket client + order execution (Phase 1.1, 1.2) | Can't do anything without this |
| P0 | Economic data scraper — Fed/CPI (Phase 2.1 Option A) | Highest-value, zero-fee markets |
| P1 | Core arb logic + risk management (Phase 3.1, 4.1) | Need decision engine to trade |
| P1 | Market scanner (Phase 1.3) | Find opportunities automatically |
| P2 | Sports data feed (Phase 2.2 Option B — free API first) | Second-best category |
| P2 | Monitoring + alerts (Phase 5.1) | Essential for live operation |
| P3 | Crypto daily/weekly markets (Phase 2.3) | Lower priority, fees are a risk |
| P3 | Backtesting framework (Phase 5.3) | Nice to have for optimization |
| P3 | Dashboard (Phase 5.2) | Nice to have |

### 6.3 — Key Metrics to Track
- **Win rate**: Target ≥ 95% (99%+ confidence threshold should deliver this)
- **Average profit per trade**: Target $20–$200 depending on market liquidity
- **Latency (event → order fill)**: Target <1s for economic data, <5s for sports
- **Daily trade count**: Expect 1–5 trades/day (event-driven, not high-frequency)
- **Monthly ROI**: Benchmark against 10–30% on deployed capital

---

## Appendix A: Key API Endpoints & Resources

| Resource | URL |
|----------|-----|
| Polymarket CLOB Docs | `https://docs.polymarket.com/developers/CLOB/introduction` |
| Polymarket Gamma API (market data) | `https://gamma-api.polymarket.com` |
| Polymarket WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| py-clob-client (Python) | `https://github.com/Polymarket/py-clob-client` |
| Polymarket Agents (AI trading) | `https://github.com/Polymarket/agents` |
| UMA Oracle Docs | `https://docs.polymarket.com/developers/resolution/UMA` |
| AlphaFlash (econ data) | `https://alphaflash.com` |
| Binance WebSocket (crypto) | `wss://stream.binance.com:9443/ws/` |
| BLS Data Releases | `https://www.bls.gov/schedule/news_release.htm` |
| Fed Calendar | `https://www.federalreserve.gov/newsevents/calendar.htm` |

## Appendix B: Upcoming High-Value Events Calendar

Maintain a rolling calendar of target events. Examples of recurring opportunities:
- **FOMC Rate Decisions**: ~8 per year (next dates on Fed calendar)
- **CPI Release**: Monthly, 8:30 AM ET (BLS schedule)
- **Non-Farm Payrolls**: First Friday of each month, 8:30 AM ET
- **GDP Advance Estimate**: Quarterly
- **NBA/NFL/NHL Games**: Daily during season (check Polymarket sports section)

---

## Appendix C: Risk Acknowledgments

- **Oracle manipulation**: UMA voting is concentrated; incorrect resolutions have occurred on markets >$7M
- **Regulatory risk**: Nevada has issued a temporary ban; state-level enforcement is active and evolving
- **Competition**: Top arb wallets already extract $1.4M+/year; edge is shrinking
- **Smart contract risk**: CTF Exchange on Polygon is battle-tested but not risk-free
- **Capital risk**: Despite 99% confidence threshold, edge cases (ambiguous resolution, oracle disputes) can cause total loss on individual positions

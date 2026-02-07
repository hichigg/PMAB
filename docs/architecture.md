# Architecture

## Overview

The Polymarket Latency Arbitrage Bot exploits the structural time gap between real-world events becoming publicly known and Polymarket prediction market prices adjusting. When a data release (CPI, game score, crypto price) resolves a market's outcome, the bot detects it faster than market participants and executes trades on the correct side before prices reflect reality.

## System Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        Data Feeds                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────────┐   │
│  │ Economic │  │  Sports  │  │          Crypto              │   │
│  │ (BLS API)│  │ (ESPN)   │  │ (Binance/Coinbase/Kraken WS) │   │
│  └────┬─────┘  └────┬─────┘  └────────────┬─────────────────┘   │
│       │              │                     │                     │
│       └──────────────┴─────────────────────┘                     │
│                      │ FeedEvent                                 │
└──────────────────────┼───────────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Strategy Engine                              │
│  ┌──────────────┐  ┌────────────────┐  ┌─────────────────────┐  │
│  │ MarketMatcher│→ │ SignalGenerator│→ │  PositionSizer      │  │
│  │ (match event │  │ (confidence +  │  │  (Kelly / fixed)    │  │
│  │  to markets) │  │  edge check)   │  │                     │  │
│  └──────────────┘  └────────────────┘  └──────────┬──────────┘  │
│         ▲                                         │ TradeAction  │
│         │ MarketOpportunity                       ▼              │
│  ┌──────┴────────────┐             ┌──────────────────────────┐  │
│  │ OpportunityPrior. │             │ Risk Gates               │  │
│  │ (rank + cap)      │             │ (kill switch, position,  │  │
│  └───────────────────┘             │  oracle, depth, spread)  │  │
│                                    └──────────┬───────────────┘  │
└───────────────────────────────────────────────┼──────────────────┘
                                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Polymarket Client                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Order Exec   │  │  Presigner   │  │  Market Scanner      │   │
│  │ (FOK / GTC)  │  │  (EIP-712)   │  │  (discover + score)  │   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐                             │
│  │ Rate Limiter │  │  WebSocket   │                             │
│  │ (500/s burst)│  │ (orderbook)  │                             │
│  └──────────────┘  └──────────────┘                             │
└──────────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Monitoring                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Alert Disp.  │  │  Metrics     │  │  Daily Summary       │   │
│  │ (throttled)  │  │  Collector   │  │  Scheduler           │   │
│  └──────┬───────┘  └──────────────┘  └──────────────────────┘   │
│         ├──────────────┐                                        │
│  ┌──────▼─────┐ ┌──────▼─────┐                                  │
│  │  Telegram  │ │  Discord   │                                  │
│  └────────────┘ └────────────┘                                  │
└──────────────────────────────────────────────────────────────────┘
```

## Data Flow

1. **Feeds** poll external APIs (BLS, ESPN, Binance WS) and emit `FeedEvent` objects when new data is detected (CPI released, game ended, price spike).

2. **ArbEngine** receives each `FeedEvent` via registered callbacks:
   - **MarketMatcher** compares the event against tracked `MarketOpportunity` objects from the scanner. Each match includes a confidence score.
   - **OpportunityPrioritizer** ranks matches by expected profit × liquidity and applies per-event trade caps.
   - **MarketQualityFilter** pre-screens opportunities for depth, spread, dispute status, and fee rates.
   - **SignalGenerator** evaluates confidence and edge thresholds, rejecting stale or low-edge opportunities.
   - **PositionSizer** computes trade size using Kelly criterion or fixed sizing, capped by risk limits.

3. **Risk Gates** (pure functions) check the proposed `TradeAction` against: kill switch state, oracle blacklist, daily loss, position concentration, depth, spread, and fee rate. First rejection halts the trade.

4. **PolymarketClient** executes the order (FOK market order or GTC limit order) via the CLOB API. Pre-signed orders shave ~20-50ms off execution latency.

5. **RiskMonitor** records the fill, updates P&L and positions, and may auto-trigger kill switches on loss streaks or daily loss breaches.

6. **AlertDispatcher** routes events to Telegram/Discord channels with per-event-type throttling (CRITICAL events bypass throttle).

## Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `PolymarketClient` | `src/polymarket/client.py` | CLOB API wrapper (orders, orderbooks, WS) |
| `OrderPreSigner` | `src/polymarket/presigner.py` | EIP-712 pre-signed order preparation |
| `MarketScanner` | `src/polymarket/scanner.py` | Discover + score + track opportunities |
| `EconomicFeed` | `src/feeds/economic.py` | BLS API polling for CPI, NFP, etc. |
| `SportsFeed` | `src/feeds/sports.py` | ESPN scoreboard polling for game outcomes |
| `CryptoFeed` | `src/feeds/crypto.py` | Binance/Coinbase/Kraken WS for price data |
| `ArbEngine` | `src/strategy/engine.py` | Core pipeline orchestrator |
| `MarketMatcher` | `src/strategy/matcher.py` | Match feed events to market opportunities |
| `SignalGenerator` | `src/strategy/signals.py` | Confidence + edge evaluation |
| `PositionSizer` | `src/strategy/sizer.py` | Kelly / fixed position sizing |
| `RiskMonitor` | `src/risk/monitor.py` | Orchestrates risk gates + P&L + positions |
| `KillSwitchManager` | `src/risk/kill_switch.py` | Multi-trigger emergency halt |
| `OracleMonitor` | `src/risk/oracle_monitor.py` | UMA dispute tracking + whale alerts |
| `MarketQualityFilter` | `src/risk/market_quality.py` | Pre-trade market screening |
| `AlertDispatcher` | `src/monitor/dispatcher.py` | Event routing with throttling |
| `MetricsCollector` | `src/monitor/metrics.py` | Performance tracking + aggregation |
| `BacktestEngine` | `src/backtest/replay.py` | Historical scenario replay |
| `SimulatedClient` | `src/backtest/sim_client.py` | Orderbook-based fill simulation |

## Design Principles

- **Decimal everywhere**: All prices and sizes use `Decimal`, never `float`, for financial precision.
- **Event-driven callbacks**: All emitters use `list[Callable]` with sync/async support via `asyncio.iscoroutine()`.
- **Pure risk gates**: Risk checks in `src/risk/gates.py` are pure functions operating on data — no side effects, easy to test.
- **Config as code**: Pydantic models define all config with sane defaults. Per-category overrides in `config/strategies/*.yaml`.
- **Structured logging**: All decision points logged as structured JSON via `structlog`.

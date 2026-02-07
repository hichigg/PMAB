# Configuration Reference

All configuration is defined in `config/settings.yaml` with Pydantic-validated defaults. Every field has a sensible default — you only need to set your API credentials and enable the feeds you want.

## Settings File Structure

```yaml
polymarket:     # CLOB API connection
risk:           # Risk management thresholds
scanner:        # Market discovery settings
feeds:          # Data feed configuration
strategy:       # Trading strategy parameters
alerts:         # Notification channels
logging:        # Log level and format
```

## `polymarket` — CLOB API

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | str | `https://clob.polymarket.com` | CLOB API base URL |
| `ws_url` | str | `wss://ws-subscriptions-clob...` | WebSocket URL for orderbook subscriptions |
| `api_key` | str | `""` | CLOB API key |
| `api_secret` | secret | `""` | CLOB API secret |
| `api_passphrase` | secret | `""` | CLOB API passphrase |
| `private_key` | secret | `""` | Polygon EOA private key |
| `chain_id` | int | `137` | Polygon chain ID |

## `risk` — Risk Management

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_daily_loss_usd` | float | `500.0` | Daily loss limit (triggers kill switch) |
| `max_position_usd` | float | `5000.0` | Max single position size |
| `max_bankroll_pct_per_event` | float | `0.20` | Max % of bankroll per event |
| `min_profit_usd` | float | `10.0` | Minimum expected profit to trade |
| `min_confidence` | float | `0.99` | Minimum signal confidence |
| `min_orderbook_depth_usd` | float | `500.0` | Minimum orderbook depth |
| `max_spread` | float | `0.10` | Maximum bid-ask spread |
| `max_concurrent_positions` | int | `10` | Maximum open positions |
| `bankroll_usd` | float | `10000.0` | Total bankroll for sizing |
| `max_fee_rate_bps` | int | `0` | Max allowed fee rate (0 = zero-fee only) |
| `fee_override_min_profit_usd` | float | `100.0` | Allow fees if profit exceeds this |

### `risk.kill_switch`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_consecutive_losses` | int | `5` | Halt after N consecutive losses |
| `error_window_trades` | int | `10` | Window for error rate calculation |
| `max_error_rate_pct` | float | `50.0` | Max error rate % before halt |
| `connectivity_max_errors` | int | `5` | Max API errors before halt |
| `connectivity_max_latency_ms` | float | `5000.0` | Max API latency before halt |
| `oracle_blacklist_patterns` | list | `["at discretion of", ...]` | Reject markets matching these |

### `risk.oracle`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable UMA oracle monitoring |
| `max_uma_exposure_usd` | float | `2000.0` | Max exposure to UMA-resolved markets |
| `max_uma_exposure_pct` | float | `0.10` | Max % of bankroll in UMA markets |
| `whale_addresses` | list | `[]` | Known UMA whale addresses to track |
| `dispute_auto_reject` | bool | `true` | Auto-reject trades on disputed markets |
| `poll_interval_secs` | float | `30.0` | Subgraph polling interval |

## `scanner` — Market Discovery

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `scan_interval_secs` | float | `60.0` | How often to rescan markets |
| `max_tracked_markets` | int | `50` | Max markets to track simultaneously |
| `orderbook_batch_size` | int | `10` | Batch size for orderbook fetches |
| `score_weights.depth` | float | `0.4` | Weight for depth in scoring |
| `score_weights.spread` | float | `0.4` | Weight for spread in scoring |
| `score_weights.recency` | float | `0.2` | Weight for time-to-expiry |

## `feeds` — Data Sources

### `feeds.economic`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable BLS economic data feed |
| `source` | str | `bls_scraper` | Data source type |
| `poll_interval_ms` | int | `100` | Poll frequency in milliseconds |
| `indicators` | list | `[CPI, CORE_CPI, NFP, ...]` | Indicators to track |
| `base_url` | str | `https://api.bls.gov/...` | BLS API endpoint |
| `api_key` | str | `""` | Optional BLS API key |

### `feeds.sports`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable ESPN sports feed |
| `source` | str | `espn` | Data source type |
| `poll_interval_ms` | int | `10000` | Poll frequency (10s) |
| `leagues` | list | `[NFL, NBA, MLB, NHL]` | Leagues to monitor |

### `feeds.crypto`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable crypto price feeds |
| `exchanges` | list | `[binance, coinbase, kraken]` | Exchanges for cross-validation |
| `pairs` | list | `[BTC_USDT, ETH_USDT]` | Trading pairs |
| `price_move_threshold_pct` | float | `2.0` | Min price move % to emit event |
| `cross_validation_threshold_pct` | float | `1.0` | Max divergence between exchanges |

## `strategy` — Trading Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `match_confidence_threshold` | float | `0.8` | Min match confidence to proceed |
| `min_edge` | float | `0.05` | Min edge (expected profit %) |
| `min_confidence` | float | `0.99` | Min signal confidence |
| `max_staleness_secs` | float | `60.0` | Reject events older than this |
| `base_size_usd` | float | `100.0` | Base position size (fixed sizing) |
| `max_size_usd` | float | `1000.0` | Max position size |
| `kelly_fraction` | float | `0.25` | Kelly criterion fraction |
| `use_kelly_sizing` | bool | `false` | Use Kelly instead of fixed sizing |
| `default_order_type` | str | `FOK` | Default order type (FOK or GTC) |
| `max_slippage` | float | `0.02` | Max slippage tolerance |
| `use_presigned_orders` | bool | `true` | Pre-sign orders for speed |

### Per-Category Overrides

Per-category strategy configs live in `config/strategies/*.yaml`. These override the base strategy parameters for specific market categories. See `config/strategies/economic.yaml` for an example.

## `alerts` — Notifications

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `telegram.enabled` | bool | `false` | Enable Telegram alerts |
| `telegram.bot_token` | secret | `""` | Telegram bot token |
| `telegram.chat_id` | str | `""` | Telegram chat ID |
| `discord.enabled` | bool | `false` | Enable Discord alerts |
| `discord.webhook_url` | secret | `""` | Discord webhook URL |
| `throttle_secs` | float | `30.0` | Min seconds between same-type alerts |
| `daily_summary_hour_utc` | int | `0` | UTC hour for daily P&L summary |

## `logging`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `level` | str | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR |
| `format` | str | `json` | Output format: `json` or `console` |

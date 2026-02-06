# Polymarket Latency Arbitrage Bot

## What This Is
A bot that exploits the structural latency gap between real-world event outcomes becoming publicly known and Polymarket market prices catching up. It reacts to known outcomes (not predictions) — deterministic strategy, not speculative.

## Project Structure
```
src/
  core/        — Config loading, logging setup, shared types
  feeds/       — Data source connectors (economic, sports, crypto)
  polymarket/  — CLOB client wrapper, order pre-signing, market scanning
  strategy/    — Decision engine / latency arb logic per market category
  risk/        — Position limits, kill switches, oracle risk monitoring
  monitor/     — Alerts (Telegram/Discord), P&L tracking, dashboard
tests/         — Pytest test suite (mirrors src/ structure)
scripts/       — One-off utilities (market scanner, backtest, dashboard)
config/        — settings.yaml (gitignored), strategy YAML configs
docs/          — Documentation
```

## Tech Stack
- **Python 3.11+** with async/await throughout
- **py-clob-client** — Polymarket official CLOB SDK
- **websockets / aiohttp / httpx** — async networking
- **web3 / eth-account** — Polygon interaction, EIP-712 order signing
- **pydantic** — config and data validation
- **structlog** — structured JSON logging
- **pytest + pytest-asyncio** — testing
- **ruff** — linting, **mypy** — type checking

## Key Conventions
- Config lives in `config/settings.yaml` (gitignored). See `config/settings.example.yaml` for the template.
- All secrets (API keys, private keys) go in `config/settings.yaml` or `.env` — never hardcode.
- Strategy configs per market category go in `config/strategies/*.yaml`.
- Use `structlog` for all logging — no `print()` statements.
- Async-first: prefer `async def` for I/O-bound code.
- Type hints on all public functions. Run `mypy` and `ruff` before committing.

## Running
```bash
pip install -e ".[dev]"    # install with dev deps
pytest                     # run tests
ruff check src/ tests/     # lint
mypy src/                  # type check
```

## Build Priority (from action plan)
- **P0**: Polymarket CLOB client + order execution, Economic data scraper (Fed/CPI)
- **P1**: Core arb logic + risk management, Market scanner
- **P2**: Sports data feed, Monitoring + alerts
- **P3**: Crypto markets, Backtesting, Dashboard

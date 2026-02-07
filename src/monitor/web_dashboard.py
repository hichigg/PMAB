"""Lightweight web dashboard — serves a live HTML dashboard over HTTP.

Runs as an ``aiohttp`` web server alongside the paper trading bot.
Exposes:
- ``GET /``          → HTML dashboard (auto-refreshes via JS)
- ``GET /api/metrics`` → JSON metrics payload
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from typing import Any, Callable

from aiohttp import web

from src.monitor.metrics import MetricsCollector

SnapshotFn = Callable[[], dict[str, object]]


def _check_basic_auth(request: web.Request, username: str, password: str) -> bool:
    """Validate HTTP Basic Auth credentials."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        req_user, req_pass = decoded.split(":", 1)
    except Exception:
        return False
    user_ok = hmac.compare_digest(req_user, username)
    pass_ok = hmac.compare_digest(req_pass, password)
    return user_ok and pass_ok


@web.middleware
async def _auth_middleware(request: web.Request, handler: Any) -> web.Response:
    """Require HTTP Basic Auth on all routes when credentials are configured."""
    username = request.app.get("auth_username")
    password = request.app.get("auth_password")
    if username and password:
        if not _check_basic_auth(request, username, password):
            return web.Response(
                status=401,
                text="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="Polymarket Dashboard"'},
            )
    return await handler(request)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _build_metrics_json(
    collector: MetricsCollector,
    snapshot_fn: SnapshotFn | None = None,
) -> dict[str, Any]:
    summary = collector.summary()
    cat_stats = collector.category_stats()
    latency = collector.latency_percentiles()
    pnl_curve = collector.pnl_curve()
    histogram = collector.latency_histogram()
    liquidity = collector.liquidity_stats()
    risk_snap = snapshot_fn() if snapshot_fn else None

    categories = []
    for cat in sorted(cat_stats, key=lambda c: c.value):
        s = cat_stats[cat]
        categories.append({
            "name": s.category.value,
            "trades": s.total_trades,
            "wins": s.wins,
            "losses": s.losses,
            "win_rate": round(s.win_rate, 4),
            "pnl": float(s.total_profit),
            "avg": float(s.avg_profit),
        })

    pnl_points = [
        {"index": p.trade_index, "pnl": float(p.cumulative_pnl)}
        for p in pnl_curve
    ]

    hist = [
        {"lo": lo, "hi": hi, "count": count}
        for lo, hi, count in histogram
    ]

    return {
        "timestamp": time.time(),
        "summary": {k: float(v) if isinstance(v, Decimal) else v
                     for k, v in summary.items()
                     if k not in ("latency", "liquidity")},
        "categories": categories,
        "latency": latency,
        "histogram": hist,
        "pnl_curve": pnl_points,
        "liquidity": {k: float(v) if isinstance(v, Decimal) else v
                       for k, v in liquidity.items()},
        "risk": {k: float(v) if isinstance(v, Decimal) else v
                  for k, v in (risk_snap or {}).items()},
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Arb Bot — Paper Trading</title>
<style>
  :root {
    --bg: #0a0e17;
    --surface: #111827;
    --surface2: #1a2332;
    --border: #1e2d3d;
    --text: #e2e8f0;
    --dim: #64748b;
    --cyan: #22d3ee;
    --green: #34d399;
    --red: #f87171;
    --yellow: #fbbf24;
    --blue: #60a5fa;
    --purple: #a78bfa;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'JetBrains Mono', 'Fira Code', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 20px;
  }
  .header {
    text-align: center;
    padding: 24px 0 16px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }
  .header h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--cyan);
    letter-spacing: 2px;
  }
  .header .subtitle {
    color: var(--dim);
    font-size: 0.75rem;
    margin-top: 4px;
  }
  .header .paper-badge {
    display: inline-block;
    background: rgba(251,191,36,0.15);
    color: var(--yellow);
    font-size: 0.65rem;
    font-weight: 700;
    padding: 2px 10px;
    border-radius: 4px;
    border: 1px solid rgba(251,191,36,0.3);
    margin-left: 10px;
    letter-spacing: 1px;
  }
  .header .time {
    color: var(--dim);
    font-size: 0.7rem;
    margin-top: 8px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 16px;
    max-width: 1200px;
    margin: 0 auto;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    overflow: hidden;
  }
  .card.full { grid-column: 1 / -1; }
  .card-title {
    font-size: 0.65rem;
    font-weight: 700;
    color: var(--dim);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-title .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--green);
    display: inline-block;
  }
  .card-title .dot.killed { background: var(--red); }
  .big-number {
    font-size: 2rem;
    font-weight: 700;
    line-height: 1;
  }
  .big-label {
    font-size: 0.65rem;
    color: var(--dim);
    margin-top: 2px;
  }
  .stat-row {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .stat {
    display: flex;
    flex-direction: column;
  }
  .stat-value {
    font-size: 1.1rem;
    font-weight: 600;
  }
  .stat-label {
    font-size: 0.6rem;
    color: var(--dim);
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--text); }
  .warn { color: var(--yellow); }

  /* Category table */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
  }
  th {
    text-align: left;
    color: var(--dim);
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
  }
  th:not(:first-child) { text-align: right; }
  td {
    padding: 8px;
    border-bottom: 1px solid rgba(30,45,61,0.5);
  }
  td:not(:first-child) { text-align: right; }
  td:first-child {
    color: var(--cyan);
    font-weight: 600;
  }
  .empty-state {
    color: var(--dim);
    font-size: 0.8rem;
    font-style: italic;
    padding: 12px 0;
  }

  /* Bar chart */
  .bar-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0;
    font-size: 0.75rem;
  }
  .bar-label {
    width: 100px;
    text-align: right;
    color: var(--dim);
    font-size: 0.7rem;
    flex-shrink: 0;
  }
  .bar-track {
    flex: 1;
    height: 16px;
    background: var(--surface2);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--cyan), var(--blue));
    border-radius: 3px;
    transition: width 0.5s ease;
  }
  .bar-count {
    width: 30px;
    text-align: right;
    font-size: 0.7rem;
    color: var(--text);
  }

  /* Sparkline / PnL chart */
  .chart-container {
    width: 100%;
    height: 120px;
    position: relative;
  }
  .chart-container canvas {
    width: 100% !important;
    height: 100% !important;
  }
  .chart-summary {
    display: flex;
    justify-content: space-between;
    margin-top: 8px;
    font-size: 0.7rem;
    color: var(--dim);
  }

  /* Latency pills */
  .pills {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .pill {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 14px;
    text-align: center;
  }
  .pill-value {
    font-size: 1rem;
    font-weight: 700;
  }
  .pill-label {
    font-size: 0.55rem;
    color: var(--dim);
    letter-spacing: 1px;
    text-transform: uppercase;
  }

  /* Status indicator */
  .status-active { color: var(--green); }
  .status-killed { color: var(--red); }

  /* Refresh indicator */
  .refresh-bar {
    position: fixed;
    top: 0; left: 0;
    height: 2px;
    background: var(--cyan);
    transition: width 5s linear;
    z-index: 100;
  }

  @media (max-width: 768px) {
    body { padding: 10px; }
    .grid { grid-template-columns: 1fr; gap: 12px; }
    .header h1 { font-size: 1.1rem; }
    .big-number { font-size: 1.5rem; }
    .stat-row { gap: 16px; }
    .pills { gap: 6px; }
    .pill { padding: 6px 10px; }
  }
</style>
</head>
<body>

<div id="refresh-bar" class="refresh-bar" style="width:0%"></div>

<div class="header">
  <h1>POLYMARKET ARB BOT <span class="paper-badge">PAPER MODE</span></h1>
  <div class="subtitle">Performance Dashboard</div>
  <div class="time" id="last-update">Loading...</div>
</div>

<div class="grid">
  <!-- Wallet Card -->
  <div class="card">
    <div class="card-title">Wallet Balance</div>
    <div class="big-number" id="wallet">$10,000.00</div>
    <div class="big-label">Started with $10,000.00</div>
    <div class="stat-row">
      <div class="stat">
        <span class="stat-value" id="pnl">$0.00</span>
        <span class="stat-label">P&L</span>
      </div>
      <div class="stat">
        <span class="stat-value" id="pnl-pct">0.0%</span>
        <span class="stat-label">Return</span>
      </div>
      <div class="stat">
        <span class="stat-value" id="today-pnl">$0.00</span>
        <span class="stat-label">Today</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="total-trades">0</span>
        <span class="stat-label">Trades</span>
      </div>
    </div>
  </div>

  <!-- Win Rate Card -->
  <div class="card">
    <div class="card-title">Win Rate</div>
    <div class="big-number" id="win-rate">0.0%</div>
    <div class="stat-row">
      <div class="stat">
        <span class="stat-value positive" id="wins">0</span>
        <span class="stat-label">Wins</span>
      </div>
      <div class="stat">
        <span class="stat-value negative" id="losses">0</span>
        <span class="stat-label">Losses</span>
      </div>
      <div class="stat">
        <span class="stat-value" id="avg-profit">$0.00</span>
        <span class="stat-label">Avg / Trade</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="signals">0</span>
        <span class="stat-label">Signals</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="skipped">0</span>
        <span class="stat-label">Skipped</span>
      </div>
    </div>
  </div>

  <!-- Latency Card -->
  <div class="card">
    <div class="card-title">Latency</div>
    <div class="pills" id="latency-pills">
      <div class="pill"><div class="pill-value" id="lat-min">-</div><div class="pill-label">Min</div></div>
      <div class="pill"><div class="pill-value" id="lat-p50">-</div><div class="pill-label">P50</div></div>
      <div class="pill"><div class="pill-value" id="lat-p90">-</div><div class="pill-label">P90</div></div>
      <div class="pill"><div class="pill-value" id="lat-p99">-</div><div class="pill-label">P99</div></div>
      <div class="pill"><div class="pill-value" id="lat-max">-</div><div class="pill-label">Max</div></div>
    </div>
  </div>

  <!-- Risk Status Card -->
  <div class="card">
    <div class="card-title"><span class="dot" id="risk-dot"></span> Risk Status</div>
    <div class="stat-row">
      <div class="stat">
        <span class="stat-value" id="risk-status">ACTIVE</span>
        <span class="stat-label">Status</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="positions">0</span>
        <span class="stat-label">Positions</span>
      </div>
      <div class="stat">
        <span class="stat-value" id="exposure">$0.00</span>
        <span class="stat-label">Exposure</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="trades-today">0</span>
        <span class="stat-label">Today</span>
      </div>
    </div>
  </div>

  <!-- Categories Table -->
  <div class="card full">
    <div class="card-title">Categories</div>
    <div id="categories-container">
      <div class="empty-state">Waiting for first trade...</div>
    </div>
  </div>

  <!-- P&L Chart -->
  <div class="card full">
    <div class="card-title">P&L Curve</div>
    <div class="chart-container"><canvas id="pnl-chart"></canvas></div>
    <div class="chart-summary">
      <span id="pnl-low">Low: -</span>
      <span id="pnl-current">Current: -</span>
      <span id="pnl-high">High: -</span>
    </div>
  </div>

  <!-- Latency Histogram -->
  <div class="card full">
    <div class="card-title">Latency Distribution</div>
    <div id="histogram-container">
      <div class="empty-state">No data yet</div>
    </div>
  </div>

  <!-- Liquidity Card -->
  <div class="card">
    <div class="card-title">Liquidity</div>
    <div class="stat-row">
      <div class="stat">
        <span class="stat-value" id="liq-captured">$0.00</span>
        <span class="stat-label">Captured</span>
      </div>
      <div class="stat">
        <span class="stat-value neutral" id="liq-available">$0.00</span>
        <span class="stat-label">Available</span>
      </div>
      <div class="stat">
        <span class="stat-value warn" id="liq-ratio">0.0%</span>
        <span class="stat-label">Ratio</span>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function usd(v) {
  const n = Number(v) || 0;
  return '$' + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function pct(v) { return (Number(v)*100).toFixed(1) + '%'; }
function ms(v) {
  const n = Number(v) || 0;
  return n < 1 ? Math.round(n*1000)+'ms' : n.toFixed(2)+'s';
}
function colorClass(v) { return Number(v) >= 0 ? 'positive' : 'negative'; }
function latColor(v) {
  const n = Number(v);
  return n < 0.5 ? 'positive' : n < 2 ? 'warn' : 'negative';
}

function drawChart(canvas, points) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  ctx.clearRect(0, 0, W, H);
  if (!points.length) return;

  const vals = points.map(p => p.pnl);
  const lo = Math.min(...vals, 0);
  const hi = Math.max(...vals, 1);
  const range = hi - lo || 1;
  const pad = 4;

  // Grid lines
  ctx.strokeStyle = '#1e2d3d';
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = pad + (H - 2*pad) * (1 - i/4);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  // Zero line
  const zeroY = pad + (H - 2*pad) * (1 - (0 - lo) / range);
  ctx.strokeStyle = '#334155';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(W, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // Area fill
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  const lastVal = vals[vals.length - 1];
  if (lastVal >= 0) {
    grad.addColorStop(0, 'rgba(52, 211, 153, 0.3)');
    grad.addColorStop(1, 'rgba(52, 211, 153, 0)');
  } else {
    grad.addColorStop(0, 'rgba(248, 113, 113, 0)');
    grad.addColorStop(1, 'rgba(248, 113, 113, 0.3)');
  }

  ctx.beginPath();
  for (let i = 0; i < vals.length; i++) {
    const x = (i / (vals.length - 1 || 1)) * W;
    const y = pad + (H - 2*pad) * (1 - (vals[i] - lo) / range);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  const lastX = W, lastY = pad + (H - 2*pad) * (1 - (vals[vals.length-1] - lo) / range);
  ctx.lineTo(lastX, H);
  ctx.lineTo(0, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  for (let i = 0; i < vals.length; i++) {
    const x = (i / (vals.length - 1 || 1)) * W;
    const y = pad + (H - 2*pad) * (1 - (vals[i] - lo) / range);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.strokeStyle = lastVal >= 0 ? '#34d399' : '#f87171';
  ctx.lineWidth = 2;
  ctx.stroke();

  // End dot
  ctx.beginPath();
  ctx.arc(lastX, lastY, 4, 0, Math.PI*2);
  ctx.fillStyle = lastVal >= 0 ? '#34d399' : '#f87171';
  ctx.fill();
}

function update(data) {
  const s = data.summary || {};
  const r = data.risk || {};
  const l = data.latency || {};
  const liq = data.liquidity || {};

  // Timestamp
  const now = new Date();
  $('last-update').textContent = now.toUTCString();

  // Wallet
  const STARTING_BANKROLL = 10000;
  const pnlVal = s.cumulative_pnl || 0;
  const walletVal = STARTING_BANKROLL + pnlVal;
  const returnPct = (pnlVal / STARTING_BANKROLL) * 100;
  $('wallet').textContent = usd(walletVal);
  $('wallet').className = 'big-number ' + colorClass(pnlVal);
  $('pnl').textContent = (pnlVal >= 0 ? '+' : '') + usd(pnlVal);
  $('pnl').className = 'stat-value ' + colorClass(pnlVal);
  $('pnl-pct').textContent = (returnPct >= 0 ? '+' : '') + returnPct.toFixed(2) + '%';
  $('pnl-pct').className = 'stat-value ' + colorClass(pnlVal);
  $('today-pnl').textContent = usd(r.realized_today);
  $('today-pnl').className = 'stat-value ' + colorClass(r.realized_today);
  $('total-trades').textContent = s.total_trades || 0;

  // Win rate
  const wr = s.win_rate || 0;
  $('win-rate').textContent = pct(wr);
  $('win-rate').className = 'big-number ' + (wr >= 0.8 ? 'positive' : wr >= 0.5 ? 'warn' : 'negative');
  $('wins').textContent = s.successful_trades || 0;
  $('losses').textContent = s.failed_trades || 0;
  $('avg-profit').textContent = usd(s.avg_profit_per_trade);
  $('avg-profit').className = 'stat-value ' + colorClass(s.avg_profit_per_trade);
  $('signals').textContent = s.signals_generated || 0;
  $('skipped').textContent = s.trades_skipped || 0;

  // Latency
  ['min','p50','p90','p99','max'].forEach(k => {
    const el = $('lat-'+k);
    el.textContent = ms(l[k]);
    el.className = 'pill-value ' + latColor(l[k]);
  });

  // Risk
  const killed = r.killed || false;
  $('risk-status').textContent = killed ? 'KILLED' : 'ACTIVE';
  $('risk-status').className = 'stat-value ' + (killed ? 'status-killed' : 'status-active');
  $('risk-dot').className = 'dot' + (killed ? ' killed' : '');
  $('positions').textContent = r.open_positions || 0;
  $('exposure').textContent = usd(r.total_exposure_usd);
  $('exposure').className = 'stat-value ' + colorClass(r.total_exposure_usd);
  $('trades-today').textContent = r.trade_count_today || 0;

  // Categories
  const cats = data.categories || [];
  if (cats.length) {
    let html = '<table><thead><tr><th>Category</th><th>Trades</th><th>W</th><th>L</th><th>Win%</th><th>P&L</th><th>Avg</th></tr></thead><tbody>';
    cats.forEach(c => {
      const wrC = c.win_rate >= 0.8 ? 'positive' : c.win_rate >= 0.5 ? 'warn' : 'negative';
      const pC = colorClass(c.pnl);
      html += `<tr><td>${c.name}</td><td>${c.trades}</td><td class="positive">${c.wins}</td><td class="negative">${c.losses}</td><td class="${wrC}">${pct(c.win_rate)}</td><td class="${pC}">${usd(c.pnl)}</td><td class="${pC}">${usd(c.avg)}</td></tr>`;
    });
    html += '</tbody></table>';
    $('categories-container').innerHTML = html;
  }

  // PnL chart
  const pts = data.pnl_curve || [];
  drawChart($('pnl-chart'), pts);
  if (pts.length) {
    const vals = pts.map(p => p.pnl);
    $('pnl-low').textContent = 'Low: ' + usd(Math.min(...vals));
    $('pnl-current').textContent = 'Current: ' + usd(vals[vals.length-1]);
    $('pnl-high').textContent = 'High: ' + usd(Math.max(...vals));
  }

  // Histogram
  const hist = data.histogram || [];
  if (hist.length) {
    const maxCount = Math.max(...hist.map(h => h.count));
    let html = '';
    hist.forEach(h => {
      const pctW = maxCount ? (h.count / maxCount * 100) : 0;
      html += `<div class="bar-row"><span class="bar-label">${ms(h.lo)}-${ms(h.hi)}</span><div class="bar-track"><div class="bar-fill" style="width:${pctW}%"></div></div><span class="bar-count">${h.count}</span></div>`;
    });
    $('histogram-container').innerHTML = html;
  }

  // Liquidity
  $('liq-captured').textContent = usd(liq.total_captured_usd);
  $('liq-captured').className = 'stat-value ' + colorClass(liq.total_captured_usd);
  $('liq-available').textContent = usd(liq.total_available_usd);
  $('liq-ratio').textContent = pct(liq.capture_ratio);
}

// Refresh loop
let refreshTimer = null;
async function refresh() {
  try {
    const resp = await fetch('/api/metrics');
    const data = await resp.json();
    update(data);
  } catch(e) {
    $('last-update').textContent = 'Connection error — retrying...';
  }
  // Animate refresh bar
  const bar = $('refresh-bar');
  bar.style.transition = 'none';
  bar.style.width = '0%';
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      bar.style.transition = 'width 5s linear';
      bar.style.width = '100%';
    });
  });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


async def _handle_index(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def _handle_metrics(request: web.Request) -> web.Response:
    collector: MetricsCollector = request.app["collector"]
    snapshot_fn: SnapshotFn | None = request.app.get("snapshot_fn")
    data = _build_metrics_json(collector, snapshot_fn)
    return web.json_response(data, dumps=lambda o: json.dumps(o, cls=_DecimalEncoder))


def create_web_app(
    collector: MetricsCollector,
    snapshot_fn: SnapshotFn | None = None,
    username: str | None = None,
    password: str | None = None,
) -> web.Application:
    """Create the aiohttp web application."""
    app = web.Application(middlewares=[_auth_middleware])
    app["collector"] = collector
    app["snapshot_fn"] = snapshot_fn
    app["auth_username"] = username
    app["auth_password"] = password
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/metrics", _handle_metrics)
    return app


async def start_web_dashboard(
    collector: MetricsCollector,
    snapshot_fn: SnapshotFn | None = None,
    host: str = "0.0.0.0",
    port: int = 8080,
    username: str | None = None,
    password: str | None = None,
) -> web.AppRunner:
    """Start the web dashboard server. Returns the runner for cleanup."""
    app = create_web_app(collector, snapshot_fn, username=username, password=password)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner

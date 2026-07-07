"""Read-only web dashboard (ported concept from the June portal,
rebuilt on the new engine's schema).

Runs as its OWN process reading the SQLite database plus the
runtime_status.json the coordinator refreshes — it can never block or
crash the trading loop.

    python run_dashboard.py --db algo_trading.db --port 8080
"""

import json
import os
import sqlite3

try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    Flask = None

PAGE = """<!doctype html>
<html><head><title>StatArb Dashboard</title>
<meta charset="utf-8">
<style>
 body{font-family:system-ui,sans-serif;margin:20px;background:#111;color:#eee}
 h1{font-size:20px} h2{font-size:15px;color:#9ab;margin:18px 0 6px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:4px 8px;border-bottom:1px solid #333;text-align:right}
 th{color:#789;text-align:right} td:first-child,th:first-child{text-align:left}
 .pos{color:#4c4}.neg{color:#e55}.badge{padding:2px 8px;border-radius:4px;
 background:#233;margin-right:8px;font-size:12px}
 .halt{background:#611;color:#fbb}
</style></head><body>
<h1>Statistical Arbitrage — Live Dashboard</h1>
<div id="status"></div>
<h2>Open positions</h2><table id="positions"></table>
<h2>Recent trades (closed)</h2><table id="reviews"></table>
<h2>Untracked closes (reconciler ledger)</h2><table id="untracked"></table>
<script>
function cls(v){return v>=0?'pos':'neg'}
function fmt(v,d=2){return v==null?'—':Number(v).toLocaleString(undefined,
  {minimumFractionDigits:d,maximumFractionDigits:d})}
async function refresh(){
 try{
  const s=await (await fetch('api/summary')).json();
  let b=`<span class="badge">${s.mode||'?'}</span>`+
    `<span class="badge">updated ${s.updated||'—'}</span>`;
  if(s.halted)b+=`<span class="badge halt">HALTED: ${s.halt_reason}</span>`;
  for(const a of s.assets||[]){
    b+=`<span class="badge">${a.asset} z=${a.z==null?'warm-up':fmt(a.z)}`+
       ` | basis ${fmt(a.basis)} | ${a.lots_today||0}/${a.lot_target||0} lots</span>`;
  }
  b+=`<span class="badge">day P&L <span class="${cls(s.daily_pnl)}">$${fmt(s.daily_pnl,0)}</span></span>`;
  document.getElementById('status').innerHTML=b;
  const p=await (await fetch('api/positions')).json();
  document.getElementById('positions').innerHTML=
   '<tr><th>ID</th><th>Asset</th><th>Side</th><th>Lots</th><th>Entry basis</th><th>Unrealized</th><th>Age</th></tr>'+
   p.map(r=>`<tr><td>${r.position_id}</td><td>${r.asset}</td><td>${r.signal_type}</td>`+
   `<td>${fmt(r.lots)}</td><td>${fmt(r.entry_premium)}</td>`+
   `<td class="${cls(r.unrealized_pnl)}">$${fmt(r.unrealized_pnl,0)}</td><td>${r.age||''}</td></tr>`).join('');
  const t=await (await fetch('api/reviews')).json();
  document.getElementById('reviews').innerHTML=
   '<tr><th>ID</th><th>Asset</th><th>Entry z</th><th>Exit z</th><th>Lots</th><th>Net P&L</th><th>Reason</th><th>Closed</th></tr>'+
   t.map(r=>`<tr><td>${r.position_id}</td><td>${r.asset}</td><td>${fmt(r.entry_z)}</td>`+
   `<td>${fmt(r.exit_z)}</td><td>${fmt(r.lots)}</td>`+
   `<td class="${cls(r.realized_pnl)}">$${fmt(r.realized_pnl,0)}</td>`+
   `<td>${r.exit_reason||''}</td><td>${(r.closed||'').slice(0,19)}</td></tr>`).join('');
  const u=await (await fetch('api/untracked')).json();
  document.getElementById('untracked').innerHTML=
   '<tr><th>Time</th><th>Leg</th><th>Symbol</th><th>Ticket</th><th>Volume</th><th>Note</th></tr>'+
   u.map(r=>`<tr><td>${(r.timestamp||'').slice(0,19)}</td><td>${r.leg}</td>`+
   `<td>${r.symbol}</td><td>${r.ticket}</td><td>${fmt(r.volume)}</td><td>${r.note||''}</td></tr>`).join('');
 }catch(e){console.log(e)}
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


def create_app(db_path="algo_trading.db", status_path="runtime_status.json"):
    if Flask is None:
        raise RuntimeError("Flask not installed — pip install flask")
    app = Flask(__name__)

    def query(sql, args=()):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return rows

    def runtime_status():
        try:
            with open(status_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @app.route('/')
    def index():
        return render_template_string(PAGE)

    @app.route('/api/summary')
    def summary():
        return jsonify(runtime_status())

    @app.route('/api/positions')
    def positions():
        status = runtime_status()
        if 'positions' in status:
            return jsonify(status['positions'])
        rows = query("SELECT position_id, asset, signal_type, entry_premium, "
                     "unrealized_pnl FROM positions WHERE status='ACTIVE'")
        return jsonify(rows)

    @app.route('/api/reviews')
    def reviews():
        limit = min(int(request.args.get('limit', 50)), 500)
        return jsonify(query(
            "SELECT * FROM trade_review ORDER BY closed DESC LIMIT ?",
            (limit,)))

    @app.route('/api/trades')
    def trades():
        limit = min(int(request.args.get('limit', 100)), 1000)
        return jsonify(query(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)))

    @app.route('/api/untracked')
    def untracked():
        return jsonify(query(
            "SELECT * FROM untracked_closes ORDER BY timestamp DESC LIMIT 50"))

    @app.route('/api/market')
    def market():
        asset = request.args.get('asset', 'GOLD')
        limit = min(int(request.args.get('limit', 500)), 5000)
        return jsonify(query(
            "SELECT timestamp, spot_price, futures_price, actual_basis, "
            "swap_basis, swap_premium_pct, signal FROM market_data "
            "WHERE asset=? ORDER BY timestamp DESC LIMIT ?", (asset, limit)))

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="StatArb dashboard")
    parser.add_argument('--db', default='algo_trading.db')
    parser.add_argument('--status', default='runtime_status.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Warning: {args.db} not found yet — dashboard will be empty "
              f"until the coordinator runs")
    app = create_app(args.db, args.status)
    print(f"Dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()

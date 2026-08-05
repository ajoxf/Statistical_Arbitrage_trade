"""Web control panel: dashboard (in-position card), settings, analysis.

Feature port from the W3 app, adapted for MT5. Runs as its OWN
process; it talks to the coordinator only through files, so it can
never block or crash the trading loop:

- reads  : SQLite DB + runtime_status.json (coordinator refreshes)
- writes : config.json  (coordinator hot-reloads safe sections ~10s;
           structural changes — accounts, legs, symbols, hedge ratio —
           are refused while running and need a restart)
           control.json (algo start/stop + manual close commands)

    python run_dashboard.py --config config.json --db algo_trading.db
"""

import json
import os
import sqlite3
import time

try:
    from flask import (Flask, jsonify, redirect, render_template_string,
                       request)
except ImportError:
    Flask = None

BASE_CSS = """
 body{font-family:system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3}
 nav{background:#161b22;padding:10px 20px;border-bottom:1px solid #30363d}
 nav a{color:#8b949e;text-decoration:none;margin-right:18px;font-weight:600}
 nav a.active,nav a:hover{color:#58a6ff}
 .wrap{max-width:1200px;margin:18px auto;padding:0 16px}
 h1{font-size:19px}h2{font-size:14px;color:#8b949e;margin:20px 0 8px;
   text-transform:uppercase;letter-spacing:.5px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:5px 8px;border-bottom:1px solid #21262d;text-align:right}
 th{color:#8b949e}td:first-child,th:first-child{text-align:left}
 .pos{color:#3fb950}.neg{color:#f85149}
 .badge{display:inline-block;padding:2px 10px;border-radius:12px;
   background:#21262d;margin:0 6px 6px 0;font-size:12px}
 .halt{background:#67060c;color:#ffb3ba}.on{background:#0f5323;color:#7ee2a8}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;
   padding:14px 16px;margin:10px 0}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
   gap:10px}
 .tile{background:#161b22;border:1px solid #30363d;border-radius:8px;
   padding:10px 12px}.tile .v{font-size:18px;font-weight:700}
 .tile .k{font-size:11px;color:#8b949e}
 .levels{display:flex;gap:18px;font-size:13px;flex-wrap:wrap}
 .levels div{text-align:center}.levels .lv{font-size:15px;font-weight:700}
 .bar{height:10px;background:#21262d;border-radius:5px;position:relative;
   overflow:hidden;margin-top:6px}
 .bar i{position:absolute;top:0;bottom:0;display:block}
 label{display:block;font-size:12px;color:#8b949e;margin:8px 0 2px}
 input,select{width:100%;box-sizing:border-box;background:#0d1117;
   color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px}
 input[type=checkbox]{width:auto}
 .row{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
   gap:10px}
 button{background:#238636;color:#fff;border:0;border-radius:6px;
   padding:8px 16px;font-weight:600;cursor:pointer;margin-top:12px}
 button.warn{background:#8b1a1a}button.gray{background:#30363d}
 .note{font-size:12px;color:#8b949e}
 #msg{position:fixed;top:12px;right:12px;background:#238636;color:#fff;
   padding:8px 14px;border-radius:6px;display:none;z-index:9}
 canvas{width:100%;background:#161b22;border:1px solid #30363d;
   border-radius:8px}
"""

NAV = """<nav><a href="/" class="{d}">Dashboard</a>
<a href="/settings" class="{s}">Settings</a>
<a href="/analysis" class="{a}">Analysis</a>
<span style="float:right;color:#8b949e;font-size:12px">StatArb MT5</span>
</nav><div id="msg"></div>"""


def page(body, active=''):
    nav = NAV.format(d='active' if active == 'd' else '',
                     s='active' if active == 's' else '',
                     a='active' if active == 'a' else '')
    return (f"<!doctype html><html><head><title>StatArb MT5</title>"
            f"<meta charset='utf-8'><style>{BASE_CSS}</style></head>"
            f"<body>{nav}<div class='wrap'>{body}</div></body></html>")


# ---------------------------------------------------------------------------
# Dashboard (in-position card)
# ---------------------------------------------------------------------------

DASHBOARD = """
<h1>Dashboard <span id="mode" class="badge"></span>
<span id="algo" class="badge"></span><span id="halt"></span>
<button id="toggle" class="gray" style="margin:0;float:right"></button></h1>
<div id="assets"></div>
<div id="poscards"></div>
<h2>MT5 self-tests</h2>
<div class="card">
 <button class="gray" style="margin:0" onclick="runTest('connectivity')">
  Connectivity test</button>
 <button class="warn" style="margin:0 0 0 8px" onclick="runTest('orders')">
  Order round-trip test</button>
 <span class="note" style="margin-left:10px">Connectivity: ping, account,
 symbols, ticks per leg. Order test places REAL minimum-volume orders
 (far limit place→cancel + market open→close by ticket) — algo must be
 stopped and the book flat.</span>
 <table id="testResults" style="margin-top:10px"></table>
</div>
<h2>Manual spread trade</h2>
<div class="card"><div class="row">
 <div><label>Asset</label><select id="m_asset"></select></div>
 <div><label>Direction</label><select id="m_dir">
  <option value="SELL_BASIS">SELL basis (short spread)</option>
  <option value="BUY_BASIS">BUY basis (long spread)</option></select></div>
 <div><label>Lots (blank = clip size)</label><input id="m_lots"></div>
 <div><button style="margin-top:20px" onclick="manualOpen()">Open pair
  </button></div>
</div><div class="note">Bypasses the signal gates only — risk limits,
circuit breakers and atomic pre-checks still apply, and the exit
ladder manages the trade like any other.</div></div>
<h2>Spread & z-score</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
<canvas id="spreadChart" height="160"></canvas>
<canvas id="zChart" height="160"></canvas></div>
<h2>Recent trades</h2><table id="recent"></table>
<script>
function cls(v){return v>=0?'pos':'neg'}
function fmt(v,d=2){return v==null?'—':Number(v).toLocaleString(undefined,
 {minimumFractionDigits:d,maximumFractionDigits:d})}
function lvl(l,k){return (l&&l[k]!=null)?fmt(l[k]):'—'}
let S={};
async function refresh(){
 try{
  S=await (await fetch('api/summary')).json();
  document.getElementById('mode').textContent=S.mode||'?';
  const alg=document.getElementById('algo');
  alg.textContent=S.algo_enabled?'ALGO ON':'ALGO OFF';
  alg.className='badge '+(S.algo_enabled?'on':'halt');
  document.getElementById('toggle').textContent=
    S.algo_enabled?'Stop algo':'Start algo';
  document.getElementById('halt').innerHTML=
    S.halted?`<span class="badge halt">HALTED: ${S.halt_reason}</span>`:'';
  document.getElementById('assets').innerHTML=(S.assets||[]).map(a=>
   `<span class="badge">${a.asset} z=${a.z==null?'warm-up':fmt(a.z)} |
    basis ${fmt(a.basis)} | ${a.lots_today||0}/${a.lot_target||0} lots</span>`
  ).join('')+`<span class="badge">day P&L
    <span class="${cls(S.daily_pnl)}">$${fmt(S.daily_pnl,0)}</span></span>`;
  const ma=document.getElementById('m_asset');
  if(!ma.options.length&&(S.assets||[]).length)
   ma.innerHTML=S.assets.map(a=>`<option>${a.asset}</option>`).join('');
  renderCards(S.positions||[]);
  renderTests(S.test_results);
 }catch(e){}
}
function renderTests(t){
 const el=document.getElementById('testResults');
 if(!t){el.innerHTML='';return}
 el.innerHTML=`<tr><th colspan=4>${t.kind} test @ ${t.ts}</th></tr>`+
  '<tr><th>Leg</th><th>Check</th><th>Result</th><th>Detail</th></tr>'+
  t.results.map(r=>`<tr><td>${r.leg}</td><td>${r.check}</td>
   <td class="${r.ok?'pos':'neg'}">${r.ok?'PASS':'FAIL'}</td>
   <td>${r.detail||''}</td></tr>`).join('');
}
async function runTest(kind){
 if(kind=='orders'&&!confirm('This places REAL minimum-volume orders '+
  'on the connected MT5 accounts (far limit place/cancel + market '+
  'open/close). Algo must be stopped and book flat. Continue?'))return;
 await fetch('api/engine/test',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({kind})});
 msg('Test requested — results appear below within ~5s');
}
function renderCards(ps){
 const el=document.getElementById('poscards');
 if(!ps.length){el.innerHTML='<div class="card note">No open position</div>';return}
 el.innerHTML=ps.map(p=>{
  const l=p.levels||{}, tp=p.tp_usd||0, st=p.stop_usd||0;
  const net=p.net_pnl==null?p.unrealized_pnl:p.net_pnl;
  const holdPct=p.max_hold_sec?Math.min(100,100*p.age_sec/p.max_hold_sec):0;
  const zero=st+tp>0?100*st/(st+tp):50;
  const g=p.unrealized_pnl||0;
  const w=Math.max(0,Math.min(100,zero+(g>=0?1:-1)*
     Math.min(Math.abs(g)/(g>=0?tp||1:st||1),1)*(g>=0?100-zero:zero)));
  return `<div class="card">
   <b>${p.position_id}</b> · ${p.asset} · ${p.signal_type} ·
   ${fmt(p.lots,1)} lots · age ${p.age}
   <button class="warn" style="float:right;margin:0"
     onclick="closePos('${p.position_id}')">Close (market)</button>
   <div class="levels" style="margin-top:10px">
    <div><div class="k">Entry sprd</div><div class="lv">${lvl(l,'entry_spread')}</div></div>
    <div><div class="k">BE</div><div class="lv">${lvl(l,'be')}</div><div class="note">$0 net</div></div>
    <div><div class="k">EX</div><div class="lv">${lvl(l,'ex')}</div></div>
    <div><div class="k">TP ${l.favorable=='down'?'↓':'↑'}</div>
      <div class="lv pos">${lvl(l,'tp')}</div><div class="note">+$${fmt(tp,0)}</div></div>
    <div><div class="k">SL</div><div class="lv neg">${lvl(l,'sl')}</div>
      <div class="note">−$${fmt(st,0)}</div></div>
    <div><div class="k">Gross P&L</div>
      <div class="lv ${cls(p.unrealized_pnl)}">$${fmt(p.unrealized_pnl,0)}</div></div>
    <div><div class="k">Net (−costs)</div>
      <div class="lv ${cls(net)}">$${fmt(net,0)}</div></div>
    <div><div class="k">Entry z</div><div class="lv">${fmt(p.entry_z)}</div></div>
    <div><div class="k">Peak/Trough</div>
      <div class="lv">${fmt(p.peak_pnl,0)}/${fmt(p.trough_pnl,0)}</div></div>
   </div>
   <div class="note" style="margin-top:8px">Stop ← P&L → Target</div>
   <div class="bar"><i style="left:${zero}%;width:2px;background:#8b949e"></i>
    <i style="${g>=0?`left:${zero}%;width:${w-zero}%`:`left:${w}%;width:${zero-w}%`};
      background:${g>=0?'#3fb950':'#f85149'}"></i></div>
   <div class="note" style="margin-top:8px">Max-hold:
    ${p.max_hold_sec?fmt(p.max_hold_sec/60,0)+'min':'—'} · used ${fmt(holdPct,0)}%</div>
   <div class="bar"><i style="left:0;width:${holdPct}%;
    background:${holdPct<66?'#3fb950':holdPct<100?'#d29922':'#f85149'}"></i></div>
  </div>`;}).join('');
}
async function manualOpen(){
 const asset=document.getElementById('m_asset').value,
       dir=document.getElementById('m_dir').value,
       lots=document.getElementById('m_lots').value;
 if(!asset){msg('No asset available yet');return}
 if(!confirm(`Open MANUAL ${dir} pair on ${asset}`+
   (lots?` (${lots} lots)?`:' (clip size)?')))return;
 await fetch('api/engine/open',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({asset,direction:dir,
   lots:lots?parseFloat(lots):null})});
 msg('Manual trade command sent — watch the position card');
}
async function closePos(id){
 if(!confirm('Close '+id+' at market?'))return;
 await fetch('api/engine/close',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({position_id:id})});
 msg('Close command sent');
}
document.getElementById('toggle').onclick=async()=>{
 await fetch('api/engine/toggle',{method:'POST'});msg('Toggled');refresh();};
function msg(t){const m=document.getElementById('msg');m.textContent=t;
 m.style.display='block';setTimeout(()=>m.style.display='none',2500)}
function drawLine(id,vals,color,hline){
 const c=document.getElementById(id),x=c.getContext('2d');
 const W=c.width=c.clientWidth,H=c.height;
 x.clearRect(0,0,W,H);if(!vals||vals.length<2)return;
 const v=vals.filter(a=>a!=null);if(v.length<2)return;
 const mn=Math.min(...v),mx=Math.max(...v),sp=(mx-mn)||1;
 if(hline!=null){x.strokeStyle='#30363d';x.beginPath();
  const hy=H-8-(hline-mn)/sp*(H-16);x.moveTo(0,hy);x.lineTo(W,hy);x.stroke();}
 x.strokeStyle=color;x.lineWidth=1.5;x.beginPath();
 vals.forEach((p,i)=>{if(p==null)return;
  const px=i/(vals.length-1)*W,py=H-8-(p-mn)/sp*(H-16);
  i?x.lineTo(px,py):x.moveTo(px,py)});
 x.stroke();
 x.fillStyle='#8b949e';x.font='11px sans-serif';
 x.fillText(fmt(mx),4,12);x.fillText(fmt(mn),4,H-2);}
async function charts(){
 try{
  const a=(S.assets&&S.assets[0])?S.assets[0].asset:'GOLD';
  const m=await (await fetch(`api/market?asset=${a}&limit=600`)).json();
  m.reverse();
  drawLine('spreadChart',m.map(r=>r.actual_basis-r.swap_basis),'#58a6ff',0);
  drawLine('zChart',m.map(r=>r.z),'#d29922',0);
  const t=await (await fetch('api/reviews?limit=10')).json();
  document.getElementById('recent').innerHTML=
   '<tr><th>ID</th><th>Asset</th><th>Side</th><th>Lots</th><th>P&L</th>'+
   '<th>Reason</th><th>Outcome</th><th>Closed</th></tr>'+
   t.map(r=>`<tr><td>${r.position_id}</td><td>${r.asset}</td><td></td>
    <td>${fmt(r.lots,1)}</td>
    <td class="${cls(r.realized_pnl)}">$${fmt(r.realized_pnl,0)}</td>
    <td>${r.exit_reason||''}</td><td>${r.outcome||''}</td>
    <td>${(r.closed||'').slice(5,16)}</td></tr>`).join('')||'';
 }catch(e){}}
refresh();charts();setInterval(refresh,2000);setInterval(charts,10000);
</script>"""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# (section, key, label, kind) kind: num|bool|text|select:opts
SETTINGS_FIELDS = [
    ('SIGNALS', [
        ('USE_Z_SIGNALS', 'Z-score signals (off = fixed premium)', 'bool'),
        ('ENTRY_Z', 'Entry z', 'num'), ('EXIT_Z', 'Exit z band', 'num'),
        ('MAX_ENTRY_Z', 'Entry ceiling (max z)', 'num'),
        ('STOP_Z', 'z-stop threshold', 'num'),
        ('EXIT_MODE', 'Exit trigger mode', 'select:zscore,spread,hybrid'),
        ('LOOKBACK_SEC', 'Lookback (sec)', 'num'),
        ('STATS_INTERVAL_SEC', 'Stats refresh (sec)', 'num'),
        ('MIN_SAMPLES', 'Warm-up samples', 'num'),
        ('TREND_FILTER', 'Trend-direction filter', 'bool'),
        ('TREND_WINDOW_SEC', 'Trend window (sec)', 'num'),
        ('ENTRY_COOLDOWN_SEC', 'Entry cooldown (sec)', 'num'),
        ('STOP_COOLDOWN_SEC', 'Stop cooldown (sec)', 'num'),
    ]),
    ('EXITS', [
        ('USE_SIGMA_TARGET', 'σ-fraction take-profit', 'bool'),
        ('TP_CAPITAL_PCT', 'TP % of capital (needs leverage)', 'num'),
        ('TP_USD_PER_LOT', 'TP fixed $/lot (fallback)', 'num'),
        ('COST_FLOOR_MULT', 'TP cost floor ×', 'num'),
        ('STOP_USD_PER_LOT', 'Stop $/lot', 'num'),
        ('STOP_CAPITAL_PCT', 'Stop % of capital', 'num'),
        ('RR', 'Stop = TP ÷ RR', 'num'),
        ('LEVERAGE', 'Account leverage (0 = %-forms off)', 'num'),
        ('M2M_BUFFER_PCT', 'Margin buffer %', 'num'),
        ('GATE_FLOOR_USD', 'Reversion gate floor $', 'num'),
        ('MAX_HOLD_HALF_LIVES', 'Max-hold × half-life', 'num'),
        ('MAX_HOLD_FALLBACK_MIN', 'Max-hold fallback (min)', 'num'),
        ('MAX_HOLD_PROGRESS_SUPPRESS', 'Suppress max-hold at z-progress',
         'num'),
        ('HARD_TIME_STOP_MULT', 'Hard stop × max-hold', 'num'),
        ('HARD_MAX_HOLD_MIN', 'Hard max-hold (min, e.g. 90)', 'num'),
        ('Z_STOP_EXIT_ENABLED', 'z-stop exits (keep OFF with $ stop)',
         'bool'),
    ]),
    ('COSTS', [
        ('COMMISSION_PER_LOT_SPOT', 'Spot commission $/lot round-turn',
         'num'),
        ('COMMISSION_PER_LOT_FUT', 'Futures commission $/lot round-turn',
         'num'),
        ('SPREAD_COST_FACTOR', 'Spread cost factor (limit fills < 1)',
         'num'),
        ('MIN_EDGE_MULTIPLE', 'Edge filter: capture ≥ × cost', 'num'),
        ('TARGET_FRACTION', 'Target fraction of |z|·σ', 'num'),
    ]),
    ('TRADING', [
        ('CLIP_LOTS', 'Clip size (lots/leg)', 'num'),
        ('SLICE_LOTS', 'Child order size (lots)', 'num'),
        ('DAILY_LOT_TARGET', 'Daily lot target (NOT a cap)', 'num'),
        ('HEDGE_RATIO', 'Hedge ratio β (STRUCTURAL — restart)', 'num'),
        ('POLL_INTERVAL_SEC', 'Poll interval (sec)', 'num'),
    ]),
    ('EXECUTION', [
        ('ENTRY_STYLE', 'Entry order style', 'select:limit,market'),
        ('PEG_OFFSET_POINTS', 'Peg offset (points)', 'num'),
        ('REPEG_INTERVAL_SEC', 'Re-peg every (sec)', 'num'),
        ('LIMIT_TIMEOUT_SEC', 'Limit patience: first leg (sec)', 'num'),
        ('HEDGE_TIMEOUT_SEC', 'Limit patience: hedge leg (sec)', 'num'),
        ('EXIT_TIMEOUT_SEC', 'Limit patience: exits (sec)', 'num'),
        ('ON_TIMEOUT', 'On timeout', 'select:cross,abort'),
        ('MIN_MATCHED_FRACTION', 'Min matched fraction of clip', 'num'),
        ('SLIPPAGE_TOLERANCE', 'Market slippage tolerance ($)', 'num'),
    ]),
    ('RISK_LIMITS', [
        ('MAX_POSITIONS_PER_ASSET', 'Max positions / asset', 'num'),
        ('MAX_LOT_SIZE', 'Max lots / order', 'num'),
        ('MAX_DAILY_TRADES', 'Max trades / day', 'num'),
        ('DAILY_MAX_LOSS_USD', 'Daily max loss $ (0 = off)', 'num'),
        ('LOSS_STREAK_REDUCE', 'Reduce size after N losses', 'num'),
        ('STREAK_SIZE_CUT', 'Size cut fraction', 'num'),
        ('LOSS_STREAK_PAUSE', 'Pause after N losses', 'num'),
    ]),
    ('TELEGRAM', [
        ('ENABLED', 'Telegram enabled (token in .env)', 'bool'),
        ('NOTIFY_TRADES', 'Notify trades', 'bool'),
        ('NOTIFY_ERRORS', 'Notify errors', 'bool'),
        ('NOTIFY_SYSTEM', 'Notify system events', 'bool'),
        ('COMMANDS', 'Bot commands (/status …)', 'bool'),
    ]),
]

SETTINGS_JS = """
<script>
function msg(t,ok=true){const m=document.getElementById('msg');
 m.textContent=t;m.style.background=ok?'#238636':'#8b1a1a';
 m.style.display='block';setTimeout(()=>m.style.display='none',4000)}
async function save(){
 const f=document.getElementById('cfg');
 const data={sections:{},accounts:{},leg_accounts:{},secrets:{}};
 for(const el of f.querySelectorAll('[data-sec]')){
  const s=el.dataset.sec,k=el.dataset.key;
  data.sections[s]=data.sections[s]||{};
  data.sections[s][k]=el.type=='checkbox'?el.checked:
    (el.dataset.kind=='num'?parseFloat(el.value||0):el.value);}
 for(const el of f.querySelectorAll('[data-acct]')){
  const a=el.dataset.acct,k=el.dataset.key;
  data.accounts[a]=data.accounts[a]||{};
  data.accounts[a][k]=el.value||null;}
 data.leg_accounts.spot=document.getElementById('leg_spot').value;
 data.leg_accounts.futures=document.getElementById('leg_fut').value;
 data.trading_mode=document.getElementById('trading_mode').value;
 const tk=document.getElementById('tg_token').value,
       tc=document.getElementById('tg_chat').value;
 if(tk)data.secrets.TELEGRAM_BOT_TOKEN=tk;
 if(tc)data.secrets.TELEGRAM_CHAT_ID=tc;
 const r=await fetch('api/config',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 const j=await r.json();
 if(r.ok){msg(j.note||'Saved — coordinator hot-reloads within ~10s');}
 else msg(j.error||'Save failed',false);
}
function topoHint(){
 const a=document.getElementById('leg_spot').value,
       b=document.getElementById('leg_fut').value;
 document.getElementById('topo').textContent=
  a==b?'Topology: BOTH legs on one account — single terminal, no leg runners needed'
      :'Topology: two accounts — run one leg runner per account (run_leg.py), coordinator connects to both';
}
document.addEventListener('DOMContentLoaded',topoHint);
</script>"""


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

ANALYSIS = """
<h1>Analysis</h1>
<h2>Outcomes</h2><div class="grid" id="tiles1"></div>
<h2>Edge quality</h2><div class="grid" id="tiles2"></div>
<h2>Outcome tags</h2><div id="tags"></div>
<h2>Shadow "what-if-held" <span id="shadowbadge" class="badge"></span></h2>
<div id="shadowagg"></div><table id="shadow"></table>
<h2>SD-touch distribution</h2>
<div style="display:grid;grid-template-columns:280px 1fr;gap:10px">
<canvas id="sdChart" height="150"></canvas>
<table id="sdTouches"></table></div>
<h2>Excursions (MAE / MFE) — peak & trough with timing</h2>
<table id="exc"></table>
<h2>Trade journal</h2><table id="journal"></table>
<h2>Untracked closes (reconciler ledger)</h2><table id="untracked"></table>
<script>
function cls(v){return v>=0?'pos':'neg'}
function fmt(v,d=2){return v==null?'—':Number(v).toLocaleString(undefined,
 {minimumFractionDigits:d,maximumFractionDigits:d})}
function tile(k,v,c){return `<div class="tile"><div class="v ${c||''}">${v}</div>
 <div class="k">${k}</div></div>`}
async function load(){
 const s=await (await fetch('api/analysis')).json();
 document.getElementById('tiles1').innerHTML=
  tile('Total trades',s.total)+tile('Winners',s.winners,'pos')+
  tile('Losers',s.losers,'neg')+tile('Win rate',fmt(s.win_rate,1)+'%')+
  tile('Total P&L','$'+fmt(s.total_pnl,0),cls(s.total_pnl))+
  tile('Expectancy / trade','$'+fmt(s.expectancy,0),cls(s.expectancy));
 document.getElementById('tiles2').innerHTML=
  tile('Avg win','$'+fmt(s.avg_win,0),'pos')+
  tile('Avg loss','$'+fmt(s.avg_loss,0),'neg')+
  tile('Reward:Risk',fmt(s.rr,2))+tile('Profit factor',fmt(s.pf,2))+
  tile('Break-even WR',fmt(s.be_wr,1)+'%')+
  tile('Max drawdown','$'+fmt(s.max_dd,0),'neg')+
  tile('Median peak min',fmt(s.median_peak_min,0)+'m')+
  tile('P70 peak','$'+fmt(s.p70_peak,0));
 document.getElementById('tags').innerHTML=Object.entries(s.tags||{})
  .map(([k,v])=>`<span class="badge">${k}: ${v}</span>`).join('')||
  '<span class="note">No closed trades yet</span>';
 const t=await (await fetch('api/reviews?limit=200')).json();
 document.getElementById('exc').innerHTML=
  '<tr><th>ID</th><th>Asset</th><th>Peak $ (min)</th><th>Trough $ (min)</th>'+
  '<th>Final P&L</th><th>Capture %</th><th>Outcome</th></tr>'+
  t.map(r=>`<tr><td>${r.position_id}</td><td>${r.asset}</td>
   <td class="pos">${fmt(r.peak_pnl,0)} (${fmt(r.peak_min,0)}m)</td>
   <td class="neg">${fmt(r.trough_pnl,0)} (${fmt(r.trough_min,0)}m)</td>
   <td class="${cls(r.realized_pnl)}">$${fmt(r.realized_pnl,0)}</td>
   <td>${r.peak_pnl>0?fmt(100*r.realized_pnl/r.peak_pnl,0)+'%':'—'}</td>
   <td>${r.outcome||''}</td></tr>`).join('');
 document.getElementById('journal').innerHTML=
  '<tr><th>ID</th><th>Asset</th><th>Entry z</th><th>Exit z</th>'+
  '<th>Entry sprd</th><th>Exit sprd</th><th>Δ sprd</th>'+
  '<th>BE</th><th>EX</th><th>TP</th><th>SL</th>'+
  '<th>Lots</th><th>Notional</th><th>P&L</th><th>P&L %</th>'+
  '<th>Reason</th><th>Opened</th><th>Closed</th></tr>'+
  t.map(r=>{
   const ds=(r.exit_spread!=null&&r.entry_spread!=null)
     ?r.exit_spread-r.entry_spread:null;
   const pct=(r.notional&&r.realized_pnl!=null)
     ?100*r.realized_pnl/r.notional:null;
   return `<tr><td>${r.position_id}</td><td>${r.asset}</td>
   <td>${fmt(r.entry_z)}</td><td>${fmt(r.exit_z)}</td>
   <td>${fmt(r.entry_spread)}</td><td>${fmt(r.exit_spread)}</td>
   <td class="${cls(-ds)}">${ds==null?'—':fmt(ds)}</td>
   <td>${fmt(r.be_spread)}</td><td>${fmt(r.ex_spread)}</td>
   <td>${fmt(r.tp_spread)}</td><td>${fmt(r.sl_spread)}</td>
   <td>${fmt(r.lots,1)}</td><td>${fmt(r.notional,0)}</td>
   <td class="${cls(r.realized_pnl)}">$${fmt(r.realized_pnl,0)}</td>
   <td class="${cls(pct)}">${pct==null?'—':fmt(pct,3)+'%'}</td>
   <td>${r.exit_reason||''}</td><td>${(r.opened||'').slice(5,16)}</td>
   <td>${(r.closed||'').slice(5,16)}</td></tr>`}).join('');
 const sh=await (await fetch('api/shadow')).json();
 document.getElementById('shadowbadge').textContent=
  `${sh.count} completed · ${sh.active} live`;
 const a=sh.aggregates;
 document.getElementById('shadowagg').innerHTML=a?
  `<span class="badge">revert→target ${fmt(a.revert_target_rate,0)}%</span>
   <span class="badge">revert→BE ${fmt(a.revert_be_rate,0)}%</span>
   <span class="badge">median target ${fmt(a.median_target_min,0)}m</span>
   <span class="badge">median BE ${fmt(a.median_be_min,0)}m</span>
   <span class="badge">avg peak $${fmt(a.avg_peak_usd,0)}</span>`
  :'<span class="note">Aggregates appear after 5 completed shadows</span>';
 const live=sh.tracking.map(t=>`<tr><td>${t.position_id} <span class="badge on">LIVE</span></td>
  <td>${t.exit_reason||''}</td><td>${fmt(t.minutes,0)}m/${fmt(t.horizon_min,0)}m</td>
  <td class="${cls(t.net)}">$${fmt(t.net,0)}</td><td>—</td><td>—</td>
  <td class="pos">${fmt(t.peak,0)}</td><td>—</td></tr>`).join('');
 document.getElementById('shadow').innerHTML=
  '<tr><th>Trade</th><th>Exited as</th><th>What-if held</th><th>Net now/final</th>'+
  '<th>Back to BE</th><th>Hit TP</th><th>Peak</th><th>Verdict</th></tr>'+live+
  sh.completed.map(r=>`<tr><td>${r.position_id}</td>
   <td>${r.exit_reason||''} ($${fmt(r.exit_pnl,0)})</td>
   <td>${fmt(r.horizon_min,0)}m</td>
   <td class="${cls(r.what_if_net)}">$${fmt(r.what_if_net,0)}</td>
   <td>${r.hit_be_min==null?'—':fmt(r.hit_be_min,0)+'m'}</td>
   <td>${r.hit_tp_min==null?'—':fmt(r.hit_tp_min,0)+'m'}</td>
   <td class="pos">${fmt(r.peak,0)}</td>
   <td>${(r.verdict||'').replaceAll('_',' ')}</td></tr>`).join('');
 const sd=await (await fetch('api/sd-touches')).json();
 drawSdChart(sd.buckets||{});
 document.getElementById('sdTouches').innerHTML=
  '<tr><th>Time</th><th>Asset</th><th>SD level</th><th>Dir</th>'+
  '<th>z</th><th>Spread</th></tr>'+
  (sd.touches||[]).map(r=>`<tr><td>${(r.timestamp||'').slice(5,19)}</td>
   <td>${r.asset}</td><td>${r.sd_level>0?'+':''}${r.sd_level}</td>
   <td>${r.direction}</td><td>${fmt(r.zscore,2)}</td>
   <td>${fmt(r.spread,4)}</td></tr>`).join('');
 const u=await (await fetch('api/untracked')).json();
 document.getElementById('untracked').innerHTML=
  '<tr><th>Time</th><th>Leg</th><th>Symbol</th><th>Ticket</th>'+
  '<th>Volume</th><th>Note</th></tr>'+
  u.map(r=>`<tr><td>${(r.timestamp||'').slice(0,19)}</td><td>${r.leg}</td>
   <td>${r.symbol}</td><td>${r.ticket}</td><td>${fmt(r.volume)}</td>
   <td>${r.note||''}</td></tr>`).join('');
}
function drawSdChart(buckets){
 const c=document.getElementById('sdChart'),x=c.getContext('2d');
 const W=c.width=c.clientWidth,H=c.height;x.clearRect(0,0,W,H);
 const levels=[-3,-2,-1,1,2,3];
 const mx=Math.max(1,...levels.map(l=>buckets[l]||0));
 const bw=W/levels.length;
 levels.forEach((l,i)=>{
  const v=buckets[l]||0,h=(H-24)*v/mx;
  x.fillStyle=l>0?'#3fb950':'#f85149';
  x.fillRect(i*bw+6,H-16-h,bw-12,h);
  x.fillStyle='#8b949e';x.font='11px sans-serif';x.textAlign='center';
  x.fillText((l>0?'+':'')+l+'σ',i*bw+bw/2,H-4);
  if(v)x.fillText(v,i*bw+bw/2,H-20-h);});
}
load();
</script>"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def update_env_file(path, updates):
    """Merge key=value pairs into a .env file, preserving other lines.
    This is how the UI stores secrets — they never touch config.json."""
    lines = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f]
    keys = set(updates)
    kept = [line for line in lines
            if line.split('=', 1)[0].strip() not in keys]
    kept += [f"{key}={value}" for key, value in updates.items()]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    for key, value in updates.items():
        os.environ[key] = value      # visible to this process immediately


def create_app(db_path="algo_trading.db", status_path="runtime_status.json",
               config_path="config.json", control_path="control.json",
               env_path=".env"):
    if Flask is None:
        raise RuntimeError("Flask not installed — pip install flask")
    app = Flask(__name__)

    def query(sql, args=()):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            rows = []
        conn.close()
        return rows

    def runtime_status():
        try:
            with open(status_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def load_config_raw():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def write_control(update):
        try:
            with open(control_path, 'r', encoding='utf-8') as f:
                control = json.load(f)
        except (OSError, ValueError):
            control = {'algo_enabled': True}
        control.update(update)
        tmp = control_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(control, f)
        os.replace(tmp, control_path)
        return control

    # ---- pages ----

    @app.route('/')
    def dashboard():
        return page(DASHBOARD, 'd')

    @app.route('/analysis')
    def analysis():
        return page(ANALYSIS, 'a')

    @app.route('/settings')
    def settings():
        raw = load_config_raw()
        from .config import AlgoTradingConfig
        defaults = AlgoTradingConfig()
        mode = raw.get('trading_mode', 'paper')
        body = ["<h1>Settings</h1>",
                "<div class='note'>Saved settings hot-reload into the "
                "running coordinator within ~10s. Structural fields "
                "(accounts, legs, symbols, hedge ratio β) and the "
                "trading mode need a restart of the launcher. Passwords "
                "are stored in the local .env file, never in config or "
                "git.</div>", "<form id='cfg'>",
                "<h2>Trading mode</h2><div class='card'><div class='row'>",
                "<div><label>Mode (restart to apply)</label>"
                "<select id='trading_mode'>"
                f"<option {'selected' if mode == 'paper' else ''}>paper"
                "</option>"
                f"<option {'selected' if mode == 'live' else ''}>live"
                "</option></select></div></div>"
                "<div class='note'>LIVE trades real money on the "
                "configured accounts.</div></div>"]

        # Broker / leg topology
        body.append("<h2>Brokers & legs (MT5)</h2><div class='card'>")
        accounts = raw.get('accounts') or {
            'account_a': {}, 'account_b': {}}
        for name, acct in accounts.items():
            body.append(f"<b>{name}</b><div class='row'>")
            for key, label in [('terminal_path', 'MT5 terminal path'),
                               ('login', 'Login'),
                               ('server', 'Server'),
                               ('endpoint',
                                'Leg-runner endpoint (blank = in-process)')]:
                value = acct.get(key) or ''
                body.append(
                    f"<div><label>{label}</label><input data-acct='{name}' "
                    f"data-key='{key}' value='{value}'></div>")
            body.append(
                f"<div><label>MT5 password (blank = unchanged)</label>"
                f"<input type='password' data-acct='{name}' "
                f"data-key='_password' placeholder='stored in .env'></div>")
            body.append("</div><hr style='border-color:#21262d'>")
        legs = raw.get('leg_accounts', {})
        options = ''.join(f"<option value='{n}'>{n}</option>"
                          for n in accounts)
        body.append(
            "<div class='row'><div><label>Leg A (spot) account</label>"
            f"<select id='leg_spot' onchange='topoHint()'>{options}"
            "</select></div>"
            "<div><label>Leg B (futures) account</label>"
            f"<select id='leg_fut' onchange='topoHint()'>{options}"
            "</select></div></div>"
            f"<div class='note' id='topo'></div>"
            f"<script>document.addEventListener('DOMContentLoaded',()=>{{"
            f"document.getElementById('leg_spot').value="
            f"'{legs.get('spot', 'account_a')}';"
            f"document.getElementById('leg_fut').value="
            f"'{legs.get('futures', 'account_b')}';topoHint();}});</script>"
            "</div>")

        # Config sections
        json_key = {'SIGNAL_THRESHOLDS': 'signal_thresholds',
                    'RISK_LIMITS': 'risk_limits', 'EXECUTION': 'execution',
                    'TRADING': 'trading', 'SIGNALS': 'signals',
                    'COSTS': 'costs', 'EXITS': 'exits',
                    'RECONCILE': 'reconcile', 'TELEGRAM': 'telegram'}
        for section, fields in SETTINGS_FIELDS:
            merged = dict(getattr(defaults, section))
            merged.update(raw.get(json_key[section], {}))
            body.append(f"<h2>{section.replace('_', ' ').title()}</h2>"
                        f"<div class='card'><div class='row'>")
            for key, label, kind in fields:
                value = merged.get(key)
                if kind == 'bool':
                    checked = 'checked' if value else ''
                    body.append(
                        f"<div><label>{label}</label>"
                        f"<input type='checkbox' data-sec='{section}' "
                        f"data-key='{key}' data-kind='bool' {checked}></div>")
                elif kind.startswith('select:'):
                    opts = ''.join(
                        f"<option {'selected' if o == value else ''}>{o}"
                        f"</option>" for o in kind[7:].split(','))
                    body.append(
                        f"<div><label>{label}</label>"
                        f"<select data-sec='{section}' data-key='{key}' "
                        f"data-kind='text'>{opts}</select></div>")
                else:
                    body.append(
                        f"<div><label>{label}</label>"
                        f"<input data-sec='{section}' data-key='{key}' "
                        f"data-kind='num' value='{value}'></div>")
            body.append("</div></div>")

        body.append(
            "<h2>Telegram secrets</h2><div class='card'><div class='row'>"
            "<div><label>Bot token (blank = unchanged)</label>"
            "<input type='password' id='tg_token' "
            "placeholder='stored in .env'></div>"
            "<div><label>Chat ID (blank = unchanged / auto via /start)"
            "</label><input id='tg_chat' placeholder='stored in .env'>"
            "</div></div>"
            "<div class='note'>Create a bot with @BotFather, paste the "
            "token, save, then send /start to the bot.</div></div>")
        body.append("</form><button onclick='save()'>Save settings</button>"
                    + SETTINGS_JS)
        return page(''.join(body), 's')

    # ---- APIs ----

    @app.route('/api/summary')
    def api_summary():
        return jsonify(runtime_status())

    @app.route('/api/positions')
    def api_positions():
        return jsonify(runtime_status().get('positions', []))

    @app.route('/api/reviews')
    def api_reviews():
        limit = min(int(request.args.get('limit', 100)), 1000)
        return jsonify(query(
            "SELECT * FROM trade_review ORDER BY closed DESC LIMIT ?",
            (limit,)))

    @app.route('/api/untracked')
    def api_untracked():
        return jsonify(query(
            "SELECT * FROM untracked_closes ORDER BY timestamp DESC LIMIT 50"))

    @app.route('/api/sd-touches')
    def api_sd_touches():
        asset = request.args.get('asset')
        where = "WHERE asset=?" if asset else ""
        args = (asset,) if asset else ()
        rows = query(f"SELECT * FROM sd_touches {where} "
                     f"ORDER BY timestamp DESC LIMIT 500", args)
        buckets = {}
        for r in rows:
            buckets[r['sd_level']] = buckets.get(r['sd_level'], 0) + 1
        return jsonify({'touches': rows[:50], 'buckets': buckets})

    @app.route('/api/shadow')
    def api_shadow():
        rows = query("SELECT * FROM shadow_trades "
                     "ORDER BY completed DESC LIMIT 50")
        status = runtime_status().get('shadow', {})
        aggregates = None
        if len(rows) >= 5:      # W3 rule: aggregates are noise below 5
            target = [r for r in rows if r['verdict'] == 'REVERTED_TO_TARGET']
            be = [r for r in rows
                  if r['verdict'] == 'REVERTED_TO_BREAK_EVEN']
            def median(vals):
                vals = sorted(v for v in vals if v is not None)
                return vals[len(vals) // 2] if vals else None
            aggregates = {
                'revert_target_rate': 100 * len(target) / len(rows),
                'revert_be_rate': 100 * (len(target) + len(be)) / len(rows),
                'median_target_min': median([r['hit_tp_min']
                                             for r in target]),
                'median_be_min': median([r['hit_be_min']
                                         for r in target + be]),
                'avg_peak_usd': (sum(r['peak'] or 0 for r in rows)
                                 / len(rows)),
            }
        return jsonify({'completed': rows, 'count': len(rows),
                        'active': status.get('active', 0),
                        'tracking': status.get('tracking', []),
                        'aggregates': aggregates})

    @app.route('/api/market')
    def api_market():
        asset = request.args.get('asset', 'GOLD')
        limit = min(int(request.args.get('limit', 600)), 5000)
        return jsonify(query(
            "SELECT timestamp, spot_price, futures_price, actual_basis, "
            "swap_basis, swap_premium_pct, z, signal FROM market_data "
            "WHERE asset=? ORDER BY timestamp DESC LIMIT ?", (asset, limit)))

    @app.route('/api/analysis')
    def api_analysis():
        rows = query("SELECT realized_pnl, peak_pnl, peak_min, outcome "
                     "FROM trade_review WHERE realized_pnl IS NOT NULL "
                     "ORDER BY closed")
        pnls = [r['realized_pnl'] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        # Max drawdown over the cumulative P&L series
        peak = dd = run = 0.0
        for p in pnls:
            run += p
            peak = max(peak, run)
            dd = max(dd, peak - run)
        peaks = sorted(r['peak_pnl'] for r in rows
                       if r['peak_pnl'] is not None)
        peak_mins = sorted(r['peak_min'] for r in rows
                           if r['peak_min'] is not None and
                           r['realized_pnl'] > 0)
        tags = {}
        for r in rows:
            if r['outcome']:
                tags[r['outcome']] = tags.get(r['outcome'], 0) + 1
        rr = (avg_win / abs(avg_loss)) if avg_loss else 0
        return jsonify({
            'total': len(pnls), 'winners': len(wins), 'losers': len(losses),
            'win_rate': 100 * len(wins) / len(pnls) if pnls else 0,
            'total_pnl': total,
            'expectancy': total / len(pnls) if pnls else 0,
            'avg_win': avg_win, 'avg_loss': avg_loss,
            'rr': rr,
            'pf': gross_win / gross_loss if gross_loss else 0,
            'be_wr': 100 / (1 + rr) if rr else 0,
            'max_dd': dd,
            'median_peak_min': (peak_mins[len(peak_mins) // 2]
                                if peak_mins else None),
            'p70_peak': (peaks[int(0.7 * (len(peaks) - 1))]
                         if peaks else None),
            'tags': tags,
        })

    @app.route('/api/engine/toggle', methods=['POST'])
    def api_toggle():
        current = runtime_status().get('algo_enabled', True)
        control = write_control({'algo_enabled': not current})
        return jsonify({'algo_enabled': control['algo_enabled']})

    @app.route('/api/engine/close', methods=['POST'])
    def api_close():
        position_id = (request.get_json(silent=True) or {}).get('position_id')
        if not position_id:
            return jsonify({'error': 'position_id required'}), 400
        write_control({'close': {'position_id': position_id,
                                 'ts': time.time()}})
        return jsonify({'ok': True})

    @app.route('/api/engine/open', methods=['POST'])
    def api_open():
        payload = request.get_json(silent=True) or {}
        if not payload.get('asset') or not payload.get('direction'):
            return jsonify({'error': 'asset and direction required'}), 400
        write_control({'open': {'asset': payload['asset'],
                                'direction': payload['direction'],
                                'lots': payload.get('lots'),
                                'ts': time.time()}})
        return jsonify({'ok': True})

    @app.route('/api/engine/test', methods=['POST'])
    def api_test():
        kind = (request.get_json(silent=True) or {}).get('kind')
        if kind not in ('connectivity', 'orders'):
            return jsonify({'error': 'kind must be connectivity|orders'}), 400
        write_control({'test': {'kind': kind, 'ts': time.time()}})
        return jsonify({'ok': True})

    @app.route('/api/config', methods=['GET', 'POST'])
    def api_config():
        if request.method == 'GET':
            return jsonify(load_config_raw())
        payload = request.get_json(silent=True) or {}
        raw = load_config_raw()
        status = runtime_status()
        running = bool(status)
        in_trade = bool(status.get('positions'))

        note = 'Saved — coordinator hot-reloads within ~10s'
        sections = payload.get('sections', {})
        json_key = {'SIGNAL_THRESHOLDS': 'signal_thresholds',
                    'RISK_LIMITS': 'risk_limits', 'EXECUTION': 'execution',
                    'TRADING': 'trading', 'SIGNALS': 'signals',
                    'COSTS': 'costs', 'EXITS': 'exits',
                    'RECONCILE': 'reconcile', 'TELEGRAM': 'telegram'}

        # beta is STRUCTURAL: refuse while in a trade (W3 409 rule)
        new_beta = sections.get('TRADING', {}).get('HEDGE_RATIO')
        old_beta = (raw.get('trading') or {}).get('HEDGE_RATIO', 1.0)
        if new_beta is not None and new_beta != old_beta and in_trade:
            return jsonify({'error': 'Hedge ratio β change rejected: a '
                            'position is open (β recomputes the whole '
                            'spread series)'}), 409

        for section, values in sections.items():
            key = json_key.get(section)
            if not key:
                continue
            raw.setdefault(key, {})
            raw[key].update(values)   # back-fill: partial saves can't
                                      # zero fields not on the form

        # Secrets go to .env ONLY — passwords never touch config.json
        env_updates = dict(payload.get('secrets') or {})
        if payload.get('accounts'):
            for name, acct in payload['accounts'].items():
                password = (acct or {}).pop('_password', None)
                if password:
                    var = (acct.get('password_env')
                           or (raw.get('accounts', {}).get(name, {})
                               or {}).get('password_env')
                           or f"MT5_PASSWORD_{name.upper()}")
                    acct['password_env'] = var
                    env_updates[var] = password
                elif not acct.get('password_env'):
                    existing = (raw.get('accounts', {}).get(name, {})
                                or {}).get('password_env')
                    if existing:
                        acct['password_env'] = existing
        if env_updates:
            update_env_file(env_path, env_updates)
            note = ('Saved (secrets written to .env). Restart the '
                    'launcher for credential changes to take effect.')

        if payload.get('trading_mode') in ('paper', 'live'):
            if payload['trading_mode'] != raw.get('trading_mode', 'paper'):
                note = ('Saved. Trading-mode change takes effect when the '
                        'launcher is restarted.')
            raw['trading_mode'] = payload['trading_mode']

        if payload.get('accounts'):
            changed = payload['accounts'] != raw.get('accounts')
            raw['accounts'] = payload['accounts']
            if changed and running:
                note = ('Saved. Account changes need a coordinator '
                        'restart to take effect.')
        if payload.get('leg_accounts'):
            changed = payload['leg_accounts'] != raw.get('leg_accounts')
            raw['leg_accounts'] = payload['leg_accounts']
            if changed and running:
                note = ('Saved. Leg mapping changes need a coordinator '
                        'restart to take effect.')

        tmp = config_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, config_path)
        return jsonify({'ok': True, 'note': note})

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="StatArb control panel")
    parser.add_argument('--db', default='algo_trading.db')
    parser.add_argument('--status', default='runtime_status.json')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--control', default='control.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    app = create_app(args.db, args.status, args.config, args.control)
    print(f"Control panel: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()

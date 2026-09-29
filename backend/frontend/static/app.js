const $ = id => document.getElementById(id);
const fmt = v => (v === null || v === undefined || v === '') ? '—' : v;

function legs(rows, combSl, qty){
  const t = Object.entries(rows || {});
  if(!t.length) return '<tr><td colspan="7" class="dim">no position</td></tr>';
  return t.map(([right,l]) => {
    const sl = l.trail_sl ?? l.sl_level ?? combSl;
    const slDisplay = sl != null ? (typeof sl === 'number' ? sl.toFixed(2) : sl) : '—';
    const isCost = l.entry != null && sl != null && Math.abs(sl - l.entry) < 0.05;
    const isBreached = l.last_close != null && sl != null && l.last_close >= sl;
    
    // Per-leg P&L: for short option, profit = entry - last_close
    let pnlHtml = '—';
    if (l.entry != null && l.last_close != null) {
      const legPts = l.entry - l.last_close;
      const legRs = qty ? legPts * qty : null;
      const cls = legPts >= 0 ? 'ok' : 'bad';
      const sign = legPts >= 0 ? '+' : '';
      pnlHtml = `<b class="${cls}">${sign}${legPts.toFixed(2)}${legRs != null ? ` (${sign}₹${Math.round(legRs)})` : ''}</b>`;
    }

    return `<tr>
      <td><b>${right}</b></td><td>${fmt(l.strike)}</td>
      <td>${fmt(l.entry)}</td><td>${fmt(l.last_close)}</td>
      <td>${pnlHtml}</td>
      <td><b class="${isBreached ? 'bad' : (isCost ? 'ok' : '')}">${slDisplay}${isCost ? ' (Cost)' : ''}</b></td>
      <td><span class="tag ${l.is_open?'warn':'dim'}">${l.is_open?'OPEN':'closed'}</span></td>
    </tr>`;
  }).join('');
}

function fills(rows){
  if(!rows || !rows.length) return '<tr><td colspan="6" class="dim">none yet</td></tr>';
  return rows.slice(-12).reverse().map(f => `<tr>
    <td class="dim">${fmt(f.ts)}</td><td><b>${fmt(f.action)}</b></td>
    <td>${fmt(f.qty)}</td><td>${fmt(f.price)}</td>
    <td>${fmt(f.reason)}</td>
    <td><span class="tag ${f.mode==='live'?'warn':''}">${fmt(f.mode)}</span></td>
  </tr>`).join('');
}

// --- Trade journal (Excel) ----------------------------------------------
// The day's own workbook is the record; the master sheet is the report.
// /api/status already carries the live path + write counters, so this card
// needs no extra request per poll - /api/journal (a directory listing) is
// refreshed at most once a minute instead.
let journalInfo = null;
let journalFetchedAt = 0;

const leaf = p => (p || '').split(/[\\/]/).pop();

async function loadJournal(force){
  const now = Date.now();
  if(!force && journalInfo && (now - journalFetchedAt) < 60000) return;
  try{
    const r = await fetch('/api/journal');
    if(!r.ok) return;
    journalInfo = await r.json();
    journalFetchedAt = now;
  }catch(_){ /* /api/status already surfaces a lost API */ }
}

function journal(s){
  const file = leaf(s.journal_path);
  const el = $('j-file');
  el.textContent = file || 'not yet written';
  el.className = file ? 'ok' : 'dim';
  const fails = s.journal_failures || 0;
  const w = $('j-writes');
  w.textContent = (s.journal_writes || 0) + ' written / ' + fails + ' failed';
  w.className = fails ? 'bad' : '';
  if(!journalInfo) return;
  const days = journalInfo.days || [];
  $('j-days').textContent = days.length
    ? days.length + ' (latest ' + days[days.length - 1] + ')' : '0';
  const dir = journalInfo.dir || '';
  const d = $('j-dir');
  d.textContent = dir; d.title = dir;
  if(journalInfo.enabled === false){
    d.textContent = 'disabled (JOURNAL_ENABLED=false)';
    d.className = 'warn';
  }
  $('b-journal').disabled = !days.length;
  $('b-journal-master').disabled = !journalInfo.master_exists;
  $('b-journal-rebuild').disabled = !days.length;
}

async function refresh(){
  try{
    const s = await (await fetch('/api/status')).json();

    $('mode').textContent = s.paper_mode ? 'PAPER MODE' : 'LIVE MODE';
    $('mode').className = 'pill ' + (s.paper_mode ? 'paper' : 'live');
    $('armed').textContent = s.live_armed ? 'ARMED' : 'disarmed';
    $('armed').className = 'pill ' + (s.live_armed ? 'live' : '');
    $('conn').textContent = 'broker: ' + (s.session_ok ? 'connected' : 'offline');
    $('conn').className = 'pill ' + (s.session_ok ? 'paper' : '');

    $('s-status').textContent = fmt(s.status);
    $('s-day').textContent = fmt(s.day);
    $('s-phase').textContent = fmt(s.phase);
    $('s-lots').textContent = fmt(s.lots);
    const deferLabel = { quote: ' - waiting for premiums',
                         submit: ' - entry retrying',
                         rejected: ' - entry rejected' }[s.entry_deferred_reason] || '';
    $('s-spot').textContent = fmt(s.spot) +
      (s.atm ? ' (ATM ' + s.atm + (s.entry_deferred ? deferLabel : '') + ')' : '');
    const r = s.resolved;
    const legQty = r?.legs?.[0]?.order_qty || (r?.lot_size ? r.lot_size * (s.lots || 1) : 65);
    $('s-expiry').textContent = r ? fmt(r.expiry) : '—';
    $('s-lotsize').textContent = r
      ? fmt(r.lot_size) + ' (qty ' + (r.legs[0]?.order_qty ?? '—') + '/leg)'
      : '—';

    const rk = s.risk || {};
    const realizedPts = rk.daily_pnl_pts ?? 0;
    $('r-pnl').textContent = fmt(realizedPts);
    $('r-pnl').className = realizedPts < 0 ? 'bad' : 'ok';
    $('r-entries').textContent = fmt(rk.entries_today ?? 0);
    const c = s.config || {};
    $('r-maxloss').textContent = fmt(rk.max_daily_loss_pts ?? c.max_daily_loss_pts);
    $('r-maxent').textContent = fmt(c.max_entries_per_day);
    $('r-cut').textContent = fmt(c.no_new_entry_after);
    $('r-halt').innerHTML = rk.halted_reason
      ? '<span class="bad">' + rk.halted_reason + '</span>' : 'none';

    const comb = s.combined || {};
    const combSlPct = comb.sl_pct ?? s.config?.combined_sl_pct;
    let entVal = comb.entry;
    if (entVal == null && s.legs?.CE?.entry != null && s.legs?.PE?.entry != null) {
      entVal = s.legs.CE.entry + s.legs.PE.entry;
    }
    let currVal = comb.current;
    if (currVal == null && s.legs?.CE?.last_close != null && s.legs?.PE?.last_close != null) {
      currVal = s.legs.CE.last_close + s.legs.PE.last_close;
    }
    let slPts = (combSlPct != null && entVal != null) ? (entVal * combSlPct) : null;

    // Render legs table with individual P&L
    $('legs').innerHTML = legs(s.legs, slPts, legQty);

    // Calculate position P&L
    let livePnlPts = 0;
    let hasLivePnl = false;
    if (entVal != null && currVal != null) {
      livePnlPts = entVal - currVal;
      hasLivePnl = true;
    } else {
      // Fallback: sum of individual open legs
      for (const l of Object.values(s.legs || {})) {
        if (l.is_open && l.entry != null && l.last_close != null) {
          livePnlPts += (l.entry - l.last_close);
          hasLivePnl = true;
        }
      }
    }
    const totalPnlPts = livePnlPts + realizedPts;
    const totalPnlRs = totalPnlPts * legQty;
    const livePnlRs = livePnlPts * legQty;

    // Header P&L pill
    if (hasLivePnl || realizedPts !== 0) {
      const sign = totalPnlRs >= 0 ? '+' : '';
      $('header-pnl').textContent = `P&L: ${sign}₹${totalPnlRs.toFixed(2)} (${sign}${totalPnlPts.toFixed(2)} pts)`;
      $('header-pnl').className = 'pill ' + (totalPnlRs >= 0 ? 'paper' : 'live');
    } else {
      $('header-pnl').textContent = 'P&L: ₹0.00';
      $('header-pnl').className = 'pill';
    }

    // Risk Card Live MTM P&L
    if (hasLivePnl || realizedPts !== 0) {
      const sign = totalPnlRs >= 0 ? '+' : '';
      $('r-live-pnl').textContent = `${sign}₹${totalPnlRs.toFixed(2)} (${sign}${totalPnlPts.toFixed(2)} pts)`;
      $('r-live-pnl').className = totalPnlRs >= 0 ? 'ok' : 'bad';
    } else {
      $('r-live-pnl').textContent = '₹0.00 (0.0 pts)';
      $('r-live-pnl').className = 'dim';
    }

    if (combSlPct != null) {
      if (entVal != null) {
        const currStr = currVal != null ? currVal.toFixed(2) : '—';
        const entStr = entVal.toFixed(2);
        const slPtsStr = slPts.toFixed(2);
        const pctStr = (combSlPct * 100).toFixed(0);
        const posSign = livePnlPts >= 0 ? '+' : '';

        // Live Position P&L row
        $('comb-pnl-val').textContent = `${posSign}₹${livePnlRs.toFixed(2)} (${posSign}${livePnlPts.toFixed(2)} pts)`;
        $('comb-pnl-val').className = livePnlPts >= 0 ? 'ok' : 'bad';

        $('comb-curr-val').textContent = `${currStr} on ${entStr} credit`;
        $('comb-curr-val').className = livePnlPts < 0 ? 'bad' : 'ok';

        $('comb-sl-val').textContent = `${slPtsStr} (${pctStr}% of ${entStr} credit, set as SL for both legs)`;
        $('comb-sl-val').className = 'warn';

        const ceOpen = s.legs?.CE?.is_open;
        const peOpen = s.legs?.PE?.is_open;
        if (ceOpen && !peOpen) {
          const ceCost = s.legs?.CE?.sl_level ?? s.legs?.CE?.entry;
          $('comb-exit-val').textContent = `PE stopped out -> CE SL moved to Cost (${ceCost != null ? Number(ceCost).toFixed(2) : '—'})`;
          $('comb-exit-val').className = 'ok';
        } else if (peOpen && !ceOpen) {
          const peCost = s.legs?.PE?.sl_level ?? s.legs?.PE?.entry;
          $('comb-exit-val').textContent = `CE stopped out -> PE SL moved to Cost (${peCost != null ? Number(peCost).toFixed(2) : '—'})`;
          $('comb-exit-val').className = 'ok';
        } else if (!ceOpen && !peOpen) {
          $('comb-exit-val').textContent = `Both legs closed`;
          $('comb-exit-val').className = 'dim';
        } else {
          const ceHit = s.legs?.CE?.last_close != null && s.legs.CE.last_close >= slPts;
          const peHit = s.legs?.PE?.last_close != null && s.legs.PE.last_close >= slPts;
          $('comb-exit-val').textContent = `Trigger if either leg >= ${slPtsStr} (other leg moves to cost)`;
          $('comb-exit-val').className = (ceHit || peHit) ? 'bad' : '';
        }
      } else {
        $('comb-pnl-val').textContent = '—';
        $('comb-pnl-val').className = 'dim';
        $('comb-curr-val').textContent = '— (waiting for entry)';
        $('comb-curr-val').className = 'dim';
        $('comb-sl-val').textContent = `${(combSlPct * 100).toFixed(0)}% of premium`;
        $('comb-sl-val').className = 'dim';
        $('comb-exit-val').textContent = '—';
        $('comb-exit-val').className = 'dim';
      }
    } else {
      $('comb-pnl-val').textContent = '—';
      $('comb-pnl-val').className = 'dim';
      $('comb-curr-val').textContent = '—';
      $('comb-curr-val').className = 'dim';
      $('comb-sl-val').textContent = 'disabled (using leg SL)';
      $('comb-sl-val').className = 'dim';
      $('comb-exit-val').textContent = '—';
      $('comb-exit-val').className = 'dim';
    }
    $('fills').innerHTML = fills(s.fills);
    loadJournal(false);       // throttled inside; never blocks this render
    journal(s);

    $('b-start').disabled = !!s.session_running;
    $('b-arm').disabled = s.live_armed || s.paper_mode;
    $('b-disarm').disabled = !s.live_armed;
    $('clock').textContent = new Date().toLocaleString('en-IN');

    $('msg').textContent = s.error ? ('⚠ ' + s.error) : '';
  }catch(e){
    $('msg').textContent = '⚠ cannot reach API: ' + e.message;
  }
}

async function post(url){
  try{
    const r = await fetch(url,{method:'POST'});
    const j = await r.json();
    if(!r.ok) $('msg').textContent = '⚠ ' + (j.reason || r.statusText);
  }catch(e){ $('msg').textContent = '⚠ ' + e.message; }
  refresh();
}

$('b-start').onclick   = () => post('/api/start');
$('b-stop').onclick    = () => post('/api/stop');
$('b-arm').onclick     = () => {
  if(confirm('ARM LIVE TRADING?\n\nReal orders will be sent to the broker.'))
    post('/api/arm');
};
$('b-disarm').onclick  = () => post('/api/disarm');

// --- Journal buttons ---
function download(url){
  // A plain link keeps the browser's own filename + content-type handling,
  // which a fetch+blob round trip would throw away.
  const a = document.createElement('a');
  a.href = url; a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

$('b-journal').onclick        = () => download('/api/journal/daily');
$('b-journal-master').onclick = () => download('/api/journal/master');
$('b-journal-rebuild').onclick = async () => {
  try{
    const r = await fetch('/api/journal/rebuild', {method:'POST'});
    const j = await r.json();
    $('msg').textContent = r.ok
      ? 'Journal master rebuilt: ' + j.days + ' day(s) -> ' + leaf(j.master)
      : '⚠ ' + (j.reason || r.statusText);
    await loadJournal(true);
    refresh();
  }catch(e){ $('msg').textContent = '⚠ ' + e.message; }
};


// --- Login / Refresh Token Modal Logic ---
const modal = $('login-modal');
const authInput = $('auth-input');

$('b-login').onclick = async () => {
  $('msg').textContent = '';
  authInput.value = '';
  modal.style.display = 'flex';
  try {
    const res = await (await fetch('/api/login/url')).json();
    if(res.url){
      $('oauth-link').href = res.url;
    }
  } catch(e) {
    $('msg').textContent = '⚠ failed to get auth URL: ' + e.message;
  }
};

$('b-modal-cancel').onclick = () => {
  modal.style.display = 'none';
};

$('b-modal-submit').onclick = async () => {
  let val = authInput.value.trim();
  if(!val){
    alert('Please enter an auth code or paste the redirect URL.');
    return;
  }
  // If user pasted a full URL like ...?code=xyz or ...&code=xyz
  if(val.includes('code=')){
    try{
      const match = val.match(/[?&]code=([^&]+)/);
      if(match && match[1]) val = decodeURIComponent(match[1]);
    }catch(_){}
  }
  
  $('b-modal-submit').disabled = true;
  $('b-modal-submit').textContent = 'Exchanging...';
  try {
    const resp = await fetch('/api/login/code', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({auth_code: val})
    });
    const data = await resp.json();
    if(!resp.ok){
      alert('Token exchange error: ' + (data.reason || resp.statusText));
    } else {
      alert('Success: ' + (data.message || 'Token refreshed!'));
      modal.style.display = 'none';
      refresh();
    }
  } catch(err) {
    alert('Error exchanging code: ' + err.message);
  } finally {
    $('b-modal-submit').disabled = false;
    $('b-modal-submit').textContent = 'Exchange & Save';
  }
};


refresh();
setInterval(refresh, 2000);

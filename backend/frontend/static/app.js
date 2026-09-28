const $ = id => document.getElementById(id);
const fmt = v => (v === null || v === undefined || v === '') ? '—' : v;

function legs(rows){
  const t = Object.entries(rows || {});
  if(!t.length) return '<tr><td colspan="6" class="dim">no position</td></tr>';
  return t.map(([right,l]) => `<tr>
    <td><b>${right}</b></td><td>${fmt(l.strike)}</td>
    <td>${fmt(l.entry)}</td><td>${fmt(l.last_close)}</td>
    <td>${fmt(l.trail_sl ?? l.sl_level)}</td>
    <td><span class="tag ${l.is_open?'warn':'dim'}">${l.is_open?'OPEN':'closed'}</span></td>
  </tr>`).join('');
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
    $('s-expiry').textContent = r ? fmt(r.expiry) : '—';
    $('s-lotsize').textContent = r
      ? fmt(r.lot_size) + ' (qty ' + (r.legs[0]?.order_qty ?? '—') + '/leg)'
      : '—';

    const rk = s.risk || {};
    $('r-pnl').textContent = fmt(rk.daily_pnl_pts ?? 0);
    $('r-pnl').className = (rk.daily_pnl_pts||0) < 0 ? 'bad' : 'ok';
    $('r-entries').textContent = fmt(rk.entries_today ?? 0);
    const c = s.config || {};
    $('r-maxloss').textContent = fmt(rk.max_daily_loss_pts ?? c.max_daily_loss_pts);
    $('r-maxent').textContent = fmt(c.max_entries_per_day);
    $('r-cut').textContent = fmt(c.no_new_entry_after);
    $('r-halt').innerHTML = rk.halted_reason
      ? '<span class="bad">' + rk.halted_reason + '</span>' : 'none';

    $('legs').innerHTML = legs(s.legs);
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

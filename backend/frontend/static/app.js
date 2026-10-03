const $ = id => document.getElementById(id);
const fmt = v => (v === null || v === undefined || v === '') ? '—' : v;

// Track whether the user has touched any config field since last Apply.
let configDirty = false;
let configSynced = false;  // first poll populates the form

function legs(rows, combSl, qty, fillsByRight){
  const t = Object.entries(rows || {});
  if(!t.length) return '<tr><td colspan="7" class="dim">no position</td></tr>';
  return t.map(([right,l]) => {
    const sl = l.trail_sl ?? l.sl_level ?? combSl;
    const slDisplay = sl != null ? (typeof sl === 'number' ? sl.toFixed(2) : sl) : '—';
    const isCost = l.entry != null && sl != null && Math.abs(sl - l.entry) < 0.05;
    const isBreached = l.last_close != null && sl != null && l.last_close >= sl;

    // For closed legs: use actual exit fill from fills[] for accurate PnL display
    let exitPx = l.last_close;
    let exitLabel = l.last_close != null ? l.last_close.toFixed(2) : '—';
    if (!l.is_open && fillsByRight) {
      const legFills = fillsByRight[right] || [];
      const buys = legFills.filter(f => f.action === 'BUY');
      if (buys.length > 0) {
        exitPx = buys.reduce((s, f) => s + f.price, 0) / buys.length;
        exitLabel = exitPx.toFixed(2) + ' ✓';
      }
    }

    // Per-leg P&L: for short option, profit = entry - exit
    let pnlHtml = '—';
    if (l.entry != null && exitPx != null) {
      const legPts = l.entry - exitPx;
      const legRs = qty ? legPts * qty : null;
      const cls = legPts >= 0 ? 'ok' : 'bad';
      const sign = legPts >= 0 ? '+' : '';
      pnlHtml = `<b class="${cls}">${sign}${legPts.toFixed(2)}${legRs != null ? ` (${sign}₹${Math.round(legRs)})` : ''}</b>`;
    }

    return `<tr>
      <td><b>${right}</b></td><td>${fmt(l.strike)}</td>
      <td>${fmt(l.entry)}</td><td>${exitLabel}</td>
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

let lastTelegramCheck = 0;
async function updateTelegramPill() {
  const now = Date.now();
  if (now - lastTelegramCheck < 10000) return;
  lastTelegramCheck = now;
  try {
    const res = await (await fetch('/api/telegram/status')).json();
    const pill = $('telegram-pill');
    if (!pill) return;
    if (!res.configured) {
      pill.textContent = '✈️ telegram: not set';
      pill.className = 'pill';
      pill.title = 'Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to backend/.env';
    } else if (!res.enabled) {
      pill.textContent = '✈️ telegram: disabled';
      pill.className = 'pill';
      pill.title = 'Telegram disabled in config';
    } else if (res.last_error) {
      pill.textContent = '✈️ telegram: warning';
      pill.className = 'pill live';
      pill.title = 'Telegram warning: ' + res.last_error + '. Click to test.';
    } else {
      pill.textContent = '✈️ telegram: active';
      pill.className = 'pill paper';
      pill.title = 'Telegram active. Click to send test alert.';
    }
  } catch (e) {}
}

const tgPill = $('telegram-pill');
if (tgPill) {
  tgPill.addEventListener('click', async () => {
    try {
      tgPill.textContent = '✈️ testing...';
      const r = await fetch('/api/telegram/test', { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        alert('✅ Telegram test notification sent successfully! Check your Telegram chat.');
      } else {
        alert('❌ Telegram test failed: ' + (d.message || 'unknown error'));
      }
    } catch (err) {
      alert('❌ Failed to test Telegram: ' + err.message);
    } finally {
      lastTelegramCheck = 0;
      updateTelegramPill();
    }
  });
}

async function refresh(){
  try{
    updateTelegramPill();
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
    const combSlPct = comb.sl_pct ?? s.strategy_params?.combined_sl_pct ?? s.config?.combined_sl_pct;
    // Label always shows the CONFIGURED value (from runtime overrides), not the old session's
    const labelPct = s.strategy_params?.combined_sl_pct ?? combSlPct;
    $('comb-sl-pct').textContent = labelPct != null ? (labelPct * 100).toFixed(0) : '—';
    let entVal = comb.entry;
    if (entVal == null && s.legs?.CE?.entry != null && s.legs?.PE?.entry != null) {
      entVal = s.legs.CE.entry + s.legs.PE.entry;
    }
    let currVal = comb.current;
    if (currVal == null && s.legs?.CE?.last_close != null && s.legs?.PE?.last_close != null) {
      currVal = s.legs.CE.last_close + s.legs.PE.last_close;
    }
    let slPts = (combSlPct != null && entVal != null) ? (entVal * combSlPct) : null;

    // ─── Build fills index first (needed for legs table + PnL) ───────────────
    // Source of truth: fills[] has broker-confirmed prices (entry SELL + exit BUY).
    // Realized PnL = (sell_price - buy_price) for each fully closed leg.
    // Unrealized MTM = (entry - current_price) for each still-open leg.
    const fillsByRight = {};
    for (const f of (s.fills || [])) {
      const r = f.right;
      if (!fillsByRight[r]) fillsByRight[r] = [];
      fillsByRight[r].push(f);
    }

    // Render legs table with individual P&L (pass fillsByRight for accurate closed-leg exit price)
    $('legs').innerHTML = legs(s.legs, slPts, legQty, fillsByRight);

    // ─── PnL calculation ─────────────────────────────────────────────────────
    let realizedFromFills = 0;   // pts from fully closed legs (entry - exit)
    let unrealizedMtm = 0;       // pts from open legs (entry - current)
    let hasLivePnl = false;

    for (const [right, leg] of Object.entries(s.legs || {})) {
      const legFills = fillsByRight[right] || [];
      const sells = legFills.filter(f => f.action === 'SELL');
      const buys  = legFills.filter(f => f.action === 'BUY');

      if (!leg.is_open && sells.length > 0 && buys.length > 0) {
        // Closed leg: realized = avg sell - avg buy (short position: profit when sell > buy)
        const avgSell = sells.reduce((s, f) => s + f.price, 0) / sells.length;
        const avgBuy  = buys.reduce((s, f)  => s + f.price, 0) / buys.length;
        realizedFromFills += (avgSell - avgBuy);
        hasLivePnl = true;
      } else if (leg.is_open && leg.entry != null) {
        // Open leg: unrealized MTM from current price
        const currPx = leg.last_close ?? leg.entry;
        unrealizedMtm += (leg.entry - currPx);
        hasLivePnl = true;
      }
    }

    const livePnlPts = realizedFromFills + unrealizedMtm;
    const totalPnlPts = livePnlPts;
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

    // Feed intraday PnL chart tracker
    recordPnlPoint(s, totalPnlRs, totalPnlPts, hasLivePnl, realizedPts);

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

        // Show SL status (in-memory or broker)
        const brokerSlOrders = s.broker_sl_orders || {};
        const slOrderIds = Object.entries(brokerSlOrders)
          .map(([r, id]) => `${r}:${id}`).join(', ');
        const slOrderBadge = slOrderIds
          ? ` 🟢 Broker SL @ exchange [${slOrderIds}]`
          : ` 🛡️ In-memory SL (active)`;
        const slLevelStr = (entVal != null && slPts != null) ? (entVal + slPts).toFixed(2) : slPtsStr;
        $('comb-sl-val').textContent = `${slLevelStr} (+${slPtsStr} pts / ${pctStr}% of ${entStr} credit)${slOrderBadge}`;
        $('comb-sl-val').className = 'ok';

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
        $('comb-sl-val').textContent = `${(labelPct * 100).toFixed(0)}% of premium`;
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

    // --- Strategy Rules card -----------------------------------------------
    const sp = s.strategy_params || {};
    if (!configDirty) {
      // Only overwrite form fields if the user hasn't manually edited them
      const slPctVal = sp.sl_pct != null ? Math.round(sp.sl_pct * 100) : 0;
      const combSlVal = sp.combined_sl_pct != null ? Math.round(sp.combined_sl_pct * 100) : 0;
      const tgtVal = sp.target_pct != null ? Math.round(sp.target_pct * 100) : 0;

      $('cfg-lots').value = sp.lots || 1;
      $('cfg-sl').value = slPctVal;
      $('cfg-combsl').value = combSlVal;
      $('cfg-sltocost').checked = !!sp.sl_to_cost;
      $('cfg-target').value = tgtVal;
      $('cfg-entry-time').value = sp.entry_time || '09:20';
      $('cfg-sqo-time').value = sp.squareoff_time || '15:15';

      // Strategy type pills
      const isStrangle = sp.strategy_type === 'strangle' || (sp.strike_offset && sp.strike_offset > 0);
      $('cfg-type-straddle').className = isStrangle ? '' : 'active';
      $('cfg-type-strangle').className = isStrangle ? 'active' : '';
      $('cfg-offset-row').style.display = isStrangle ? 'flex' : 'none';
      if (isStrangle && sp.strike_offset) $('cfg-offset').value = sp.strike_offset;

      configSynced = true;
    }

    // Compute order qty per leg
    const lotSize = r?.lot_size || (r?.legs?.[0]?.lot_size) || 0;
    const cfgLots = parseInt($('cfg-lots').value) || 1;
    $('cfg-qty').textContent = lotSize ? `${cfgLots * lotSize} (${cfgLots}L × ${lotSize})` : '—';

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

$('b-server-restart').onclick = async () => {
  if (!confirm('Restart Python / Uvicorn server process now?\n\nThis will terminate the current process and launch a fresh server with the latest code.')) return;
  $('msg').textContent = 'Restarting server... please wait 3-5 seconds...';
  $('b-server-restart').disabled = true;
  try {
    await fetch('/api/server/restart', { method: 'POST' });
  } catch (_) {}
  
  let attempts = 0;
  const pollTimer = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch('/api/status');
      if (res.ok) {
        clearInterval(pollTimer);
        $('b-server-restart').disabled = false;
        $('msg').textContent = 'Server restarted successfully and online!';
        refresh();
      }
    } catch (_) {
      if (attempts > 15) {
        clearInterval(pollTimer);
        $('b-server-restart').disabled = false;
        $('msg').textContent = 'Server is taking longer to respond. Please refresh the page.';
      }
    }
  }, 1000);
};

$('b-server-kill').onclick = async () => {
  if (!confirm('KILL / TERMINATE Python server process?\n\nThe server will shut down completely and stop accepting connections.')) return;
  $('msg').textContent = 'Killing server process...';
  try {
    await fetch('/api/server/shutdown', { method: 'POST' });
    setTimeout(() => {
      $('msg').textContent = 'Server process terminated.';
    }, 1500);
  } catch (e) {
    $('msg').textContent = '⚠ Shutdown sent: ' + e.message;
  }
};

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


// --- Strategy Rules: mark dirty on any input change -------------------------
for (const id of ['cfg-lots','cfg-sl','cfg-combsl','cfg-target','cfg-offset','cfg-entry-time','cfg-sqo-time']) {
  $(id).addEventListener('input', () => { configDirty = true; });
  $(id).addEventListener('change', () => { configDirty = true; });
  $(id).addEventListener('focus', () => { configDirty = true; });
}
$('cfg-sltocost').addEventListener('change', () => { configDirty = true; });

// Straddle / Strangle pill toggle
$('cfg-type-straddle').onclick = () => {
  $('cfg-type-straddle').className = 'active';
  $('cfg-type-strangle').className = '';
  $('cfg-offset-row').style.display = 'none';
  configDirty = true;
};
$('cfg-type-strangle').onclick = () => {
  $('cfg-type-strangle').className = 'active';
  $('cfg-type-straddle').className = '';
  $('cfg-offset-row').style.display = 'flex';
  configDirty = true;
};

// Apply config button
$('b-apply-config').onclick = async () => {
  const isStrangle = $('cfg-type-strangle').classList.contains('active');
  const body = {
    lots: parseInt($('cfg-lots').value) || 1,
    sl_pct: (parseInt($('cfg-sl').value) || 0) / 100,
    combined_sl_pct: (parseInt($('cfg-combsl').value) || 0) / 100,
    sl_to_cost: $('cfg-sltocost').checked,
    target_pct: (parseInt($('cfg-target').value) || 0) / 100,
    strategy_type: isStrangle ? 'strangle' : 'straddle',
    strike_offset: isStrangle ? (parseInt($('cfg-offset').value) || 1) : 0,
    entry_time: $('cfg-entry-time').value || '09:20',
    squareoff_time: $('cfg-sqo-time').value || '15:15',
  };
  $('b-apply-config').disabled = true;
  $('b-apply-config').textContent = 'Saving...';
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (res.ok) {
      $('cfg-hint').textContent = '✓ Saved — applies on next session start';
      $('cfg-hint').style.color = 'var(--ok)';
      configDirty = false;
      setTimeout(() => {
        $('cfg-hint').textContent = 'Changes apply on next session start';
        $('cfg-hint').style.color = 'var(--dim)';
      }, 3000);
    } else {
      $('cfg-hint').textContent = '⚠ ' + (data.reason || 'Save failed');
      $('cfg-hint').style.color = 'var(--bad)';
    }
  } catch (e) {
    $('cfg-hint').textContent = '⚠ ' + e.message;
    $('cfg-hint').style.color = 'var(--bad)';
  } finally {
    $('b-apply-config').disabled = false;
    $('b-apply-config').textContent = 'Apply Changes';
  }
  refresh();
};


// ─── Small Intraday PnL Graph ────────────────────────────────────────────────
let pnlHistory = [];
let pnlDay = null;
let lastPnlSampleTime = 0;
let isDemoPnl = false;
let pnlHoverIdx = -1;
let demoPointsCache = null;

function getDemoPoints() {
  if (demoPointsCache) return demoPointsCache;
  const pts = [];
  const baseDate = new Date();
  baseDate.setHours(9, 20, 0, 0);
  const startMs = baseDate.getTime();
  const curve = [
    { m: 0, pnl: 0 },
    { m: 10, pnl: -120 },
    { m: 25, pnl: -80 },
    { m: 45, pnl: +180 },
    { m: 70, pnl: +420 },
    { m: 95, pnl: +680 },
    { m: 120, pnl: +550 },
    { m: 150, pnl: +890 },
    { m: 180, pnl: +1150 },
    { m: 210, pnl: +1320 },
    { m: 245, pnl: +1080 },
    { m: 280, pnl: +1420 },
    { m: 315, pnl: +1680 },
    { m: 355, pnl: +1560 },
  ];
  for (const step of curve) {
    const d = new Date(startMs + step.m * 60000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    pts.push({
      t: `${hh}:${mm}`,
      ts: d.getTime(),
      pnl: step.pnl,
      pts: step.pnl / 65,
    });
  }
  demoPointsCache = pts;
  return pts;
}

function initPnlChart() {
  const canvas = $('pnl-canvas');
  if (!canvas) return;

  const wrap = $('pnl-canvas-wrap');
  const tooltip = $('pnl-tooltip');

  function handlePointer(e) {
    const pts = isDemoPnl ? getDemoPoints() : pnlHistory;
    if (!pts || pts.length < 2) return;
    const rect = canvas.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const mouseX = clientX - rect.left;
    const padL = 8, padR = 36;
    const chartW = rect.width - padL - padR;
    const ratio = Math.max(0, Math.min(1, (mouseX - padL) / chartW));
    const idx = Math.round(ratio * (pts.length - 1));
    const pt = pts[idx];
    if (!pt) return;

    pnlHoverIdx = idx;
    drawPnlChart(pts, idx);

    tooltip.style.display = 'block';
    const sign = pt.pnl >= 0 ? '+' : '';
    const color = pt.pnl >= 0 ? 'var(--ok)' : 'var(--bad)';
    const ptsSign = (pt.pts || 0) >= 0 ? '+' : '';
    tooltip.innerHTML = `<span class="dim">${pt.t}</span> &nbsp;<b style="color:${color}">${sign}₹${pt.pnl.toFixed(2)}</b> <span class="dim">(${ptsSign}${(pt.pts || 0).toFixed(1)} pts)</span>`;

    let left = mouseX;
    if (left < 55) left = 55;
    if (left > rect.width - 55) left = rect.width - 55;
    tooltip.style.left = left + 'px';
    tooltip.style.top = '12px';
  }

  wrap.addEventListener('mousemove', handlePointer);
  wrap.addEventListener('touchmove', handlePointer, { passive: true });

  const resetHover = () => {
    pnlHoverIdx = -1;
    if (tooltip) tooltip.style.display = 'none';
    const pts = isDemoPnl ? getDemoPoints() : pnlHistory;
    drawPnlChart(pts, -1);
  };

  wrap.addEventListener('mouseleave', resetHover);
  wrap.addEventListener('touchend', resetHover);

  const demoBtn = $('b-pnl-demo');
  if (demoBtn) {
    demoBtn.onclick = (e) => {
      e.preventDefault();
      isDemoPnl = !isDemoPnl;
      demoBtn.textContent = isDemoPnl ? 'Live' : 'Demo';
      demoBtn.style.color = isDemoPnl ? 'var(--acc2)' : 'var(--fg2)';
      demoBtn.style.borderColor = isDemoPnl ? 'var(--acc-border)' : 'var(--line)';
      demoBtn.style.background = isDemoPnl ? 'var(--acc-bg)' : 'rgba(255,255,255,0.05)';
      const pts = isDemoPnl ? getDemoPoints() : pnlHistory;
      drawPnlChart(pts, -1);
    };
  }

  window.addEventListener('resize', () => {
    const pts = isDemoPnl ? getDemoPoints() : pnlHistory;
    drawPnlChart(pts, pnlHoverIdx);
  });
}

function recordPnlPoint(s, totalPnlRs, totalPnlPts, hasLivePnl, realizedPts) {
  if (isDemoPnl) return;

  const today = s.day || new Date().toISOString().slice(0, 10);
  if (pnlDay !== today) {
    pnlDay = today;
    try {
      const cached = localStorage.getItem('stratbot_pnl_' + today);
      pnlHistory = cached ? JSON.parse(cached) : [];
    } catch (_) {
      pnlHistory = [];
    }
  }

  // Merge server pnl_history if provided
  if (s.pnl_history && Array.isArray(s.pnl_history) && s.pnl_history.length > pnlHistory.length) {
    pnlHistory = s.pnl_history;
  }

  const hasTrades = hasLivePnl || (realizedPts !== 0) || (s.fills && s.fills.length > 0);
  const now = Date.now();
  const d = new Date();
  const timeStr = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');

  if (hasTrades) {
    const lastPt = pnlHistory[pnlHistory.length - 1];
    const pnlDelta = lastPt ? Math.abs(totalPnlRs - lastPt.pnl) : 999;
    if (!lastPt || (now - lastPnlSampleTime >= 3500) || pnlDelta >= 3) {
      pnlHistory.push({
        t: timeStr,
        ts: now,
        pnl: totalPnlRs,
        pts: totalPnlPts,
      });
      if (pnlHistory.length > 1500) pnlHistory.shift();
      lastPnlSampleTime = now;
      try {
        localStorage.setItem('stratbot_pnl_' + today, JSON.stringify(pnlHistory));
      } catch (_) {}
    }
  }

  if (pnlHoverIdx < 0) {
    drawPnlChart(pnlHistory, -1);
  }
}

function drawPnlChart(pts, hoverIdx) {
  const canvas = $('pnl-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  if (!width || !height) return;

  const targetW = Math.round(width * dpr);
  const targetH = Math.round(height * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }
  ctx.resetTransform?.();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const padLeft = 8;
  const padRight = 36;
  const padTop = 14;
  const padBottom = 16;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  if (!pts || pts.length < 2) {
    const yMid = padTop + chartH / 2;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padLeft, yMid);
    ctx.lineTo(width - padRight, yMid);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
    ctx.font = '9.5px JetBrains Mono, monospace';
    ctx.textAlign = 'left';
    ctx.fillText('₹0', width - padRight + 6, yMid + 3);

    ctx.textAlign = 'center';
    ctx.fillStyle = 'rgba(255, 255, 255, 0.35)';
    ctx.font = '11px Inter, sans-serif';
    ctx.fillText('Awaiting trade entry · Flat ₹0.00', padLeft + chartW / 2, yMid - 7);

    const hl = $('pnl-chart-highlow');
    if (hl) hl.textContent = 'Low: ₹0 · High: ₹0';
    const stat = $('pnl-curr-stat');
    if (stat) { stat.textContent = '₹0.00 (0.0 pts)'; stat.className = 'dim'; }
    const tag = $('pnl-status-tag');
    if (tag) { tag.textContent = 'Flat'; tag.className = 'pill'; }
    return;
  }

  let minVal = 0;
  let maxVal = 0;
  let minIdx = 0;
  let maxIdx = 0;
  for (let i = 0; i < pts.length; i++) {
    const v = pts[i].pnl;
    if (v < minVal) { minVal = v; minIdx = i; }
    if (v > maxVal) { maxVal = v; maxIdx = i; }
  }

  const span = Math.max(120, maxVal - minVal);
  const headRoom = span * 0.18;
  const yMin = minVal - headRoom;
  const yMax = maxVal + headRoom;
  const range = yMax - yMin;

  const getX = i => padLeft + (i / (pts.length - 1)) * chartW;
  const getY = val => padTop + (1 - (val - yMin) / range) * chartH;
  const yZero = getY(0);

  // 1. Zero Baseline
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.16)';
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, yZero);
  ctx.lineTo(width - padRight, yZero);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
  ctx.font = '9.5px JetBrains Mono, monospace';
  ctx.textAlign = 'left';
  ctx.fillText('₹0', width - padRight + 6, yZero + 3);

  // 2. Area Gradient
  const latestPnl = pts[pts.length - 1].pnl;
  const isProfit = latestPnl >= 0;
  const mainColor = isProfit ? '#22c55e' : '#ef4444';
  const glowRgb = isProfit ? '34, 197, 94' : '239, 68, 68';

  const grad = ctx.createLinearGradient(0, padTop, 0, padTop + chartH);
  if (isProfit) {
    grad.addColorStop(0, `rgba(${glowRgb}, 0.25)`);
    grad.addColorStop(1, `rgba(${glowRgb}, 0.01)`);
  } else {
    grad.addColorStop(0, `rgba(${glowRgb}, 0.01)`);
    grad.addColorStop(1, `rgba(${glowRgb}, 0.25)`);
  }

  ctx.beginPath();
  ctx.moveTo(getX(0), yZero);
  for (let i = 0; i < pts.length; i++) {
    ctx.lineTo(getX(i), getY(pts[i].pnl));
  }
  ctx.lineTo(getX(pts.length - 1), yZero);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 3. Curve Line
  ctx.beginPath();
  ctx.moveTo(getX(0), getY(pts[0].pnl));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(getX(i), getY(pts[i].pnl));
  }
  ctx.strokeStyle = mainColor;
  ctx.lineWidth = 2.2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.shadowColor = `rgba(${glowRgb}, 0.5)`;
  ctx.shadowBlur = 6;
  ctx.stroke();
  ctx.shadowBlur = 0;

  // 4. Peak / Trough Dots
  if (pts.length > 5 && maxVal > 0) {
    ctx.fillStyle = '#22c55e';
    ctx.beginPath();
    ctx.arc(getX(maxIdx), getY(maxVal), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }
  if (pts.length > 5 && minVal < 0) {
    ctx.fillStyle = '#ef4444';
    ctx.beginPath();
    ctx.arc(getX(minIdx), getY(minVal), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // 5. Active / Hovered Point
  const activeIdx = (hoverIdx !== undefined && hoverIdx >= 0) ? hoverIdx : pts.length - 1;
  const activePt = pts[activeIdx];
  const activeX = getX(activeIdx);
  const activeY = getY(activePt.pnl);

  if (hoverIdx !== undefined && hoverIdx >= 0) {
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.25)';
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(activeX, padTop);
    ctx.lineTo(activeX, padTop + chartH);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.fillStyle = `rgba(${glowRgb}, 0.35)`;
  ctx.beginPath();
  ctx.arc(activeX, activeY, 6, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = mainColor;
  ctx.beginPath();
  ctx.arc(activeX, activeY, 3, 0, Math.PI * 2);
  ctx.fill();

  // 6. Meta Readouts
  const sign = latestPnl >= 0 ? '+' : '';
  const lastPts = pts[pts.length - 1].pts || (latestPnl / 65);
  const ptsSign = lastPts >= 0 ? '+' : '';

  const hl = $('pnl-chart-highlow');
  if (hl) {
    hl.innerHTML = `Low: <span class="${minVal < 0 ? 'bad' : ''}">₹${Math.round(minVal)}</span> · High: <span class="${maxVal > 0 ? 'ok' : ''}">₹${Math.round(maxVal)}</span>`;
  }
  const stat = $('pnl-curr-stat');
  if (stat) {
    stat.innerHTML = `<span class="${latestPnl >= 0 ? 'ok' : 'bad'}">${sign}₹${latestPnl.toFixed(2)} (${ptsSign}${lastPts.toFixed(1)} pts)</span>`;
  }
  const tag = $('pnl-status-tag');
  if (tag) {
    tag.textContent = `${sign}₹${Math.round(latestPnl)}`;
    tag.className = `pill ${latestPnl >= 0 ? 'paper' : 'live'}`;
  }

  if (pts[0]?.t && $('pnl-time-start')) $('pnl-time-start').textContent = pts[0].t;
  if (pts[pts.length - 1]?.t && $('pnl-time-end')) $('pnl-time-end').textContent = pts[pts.length - 1].t;
}

// Initialize chart & kick off poll
initPnlChart();
refresh();
setInterval(refresh, 2000);


/* ============================================================
   LedgerSpy — HEURISTIC PREVIEW engine (browser)
   ============================================================
   What this is:  hand-written feature rules + hand-set weights.
   What it is NOT: a trained or calibrated ML model. No model file
                   is loaded; nothing here was learned from data.

   P1 (Truthful Baseline) changes:
   - Ground-truth/label columns (isFraud, …) are excluded from
     mapping, features and scoring (no label leakage).
   - Tier thresholds come from LS_CONFIG (shared with the chatbot).
   - Every score carries an exact points breakdown (pre-cap).
   - Benford digit statistics are exposed for an honest chart.
   - No randomness; output is deterministic.

   This file is scheduled for replacement in roadmap phase P9 by
   the Python risk engine (one source of truth).
   ============================================================ */

/* ---- Global engine state ---- */
window._mlResults = [];
window._mlSummary = null;
window._mlBenford = null;
window._selectedTxIdx = -1;

/* ============================================================
   1. STRING SIMILARITY (character-overlap heuristic)
   NOTE: this is NOT Python's SequenceMatcher; it is a crude
   bag-of-characters overlap. Replaced by entity resolution in P12.
   ============================================================ */
function stringSimilarity(a, b){
  if(!a || !b) return 0;
  a = a.toLowerCase().trim(); b = b.toLowerCase().trim();
  if(a === b) return 1;
  if(a.length === 0 || b.length === 0) return 0;
  const short = a.length <= b.length ? a : b;
  const long  = a.length <= b.length ? b : a;
  let matches = 0;
  const used = new Array(long.length).fill(false);
  for(let i = 0; i < short.length; i++){
    for(let j = 0; j < long.length; j++){
      if(!used[j] && short[i] === long[j]){ matches++; used[j] = true; break; }
    }
  }
  return (2.0 * matches) / (a.length + b.length);
}

/* ============================================================
   2. COLUMN DETECTION — maps CSV headers to engine fields.
   Label columns are never eligible (see LS_CONFIG.labelColumns).
   ============================================================ */
const ML_COLS = {
  step:       ['step','time','hour','period','day'],
  type:       ['type','transaction_type','txtype','tx_type','category'],
  amount:     ['amount','amt','value','total','debit','credit','amount(inr)','amount_(₹)','sum'],
  nameOrig:   ['nameorig','name_orig','sender','from','payer','source','nameorg','originator'],
  nameDest:   ['namedest','name_dest','receiver','to','payee','destination',
               'beneficiary','vendor','party','description','name'],
  oldBalOrig: ['oldbalanceorg','old_balance_orig','oldbalorg','sender_old_balance',
               'old_bal_orig','opening_balance'],
  newBalOrig: ['newbalanceorig','new_balance_orig','newbalorig','sender_new_balance',
               'new_bal_orig','closing_balance'],
  oldBalDest: ['oldbalancedest','old_balance_dest','oldbaldest','receiver_old_balance'],
  newBalDest: ['newbalancedest','new_balance_dest','newbaldest','receiver_new_balance'],
};

function findMLColIdx(headers){
  const map = {};
  const hLower = headers.map(h => String(h || '').toLowerCase().trim().replace(/\s+/g,'_'));
  const eligible = i => hLower[i] !== '' && !lsIsLabelColumn(headers[i]);
  Object.entries(ML_COLS).forEach(([key, aliases]) => {
    let idx = hLower.findIndex((h, i) => eligible(i) && aliases.includes(h));
    // Fallback: partial match, only for aliases/headers long enough to be meaningful
    // (prevents e.g. 'to' matching 'total' or an empty index header matching everything).
    if(idx === -1) idx = hLower.findIndex((h, i) => eligible(i) &&
      aliases.some(a => a.length >= 4 && h.length >= 4 && (h.includes(a) || a.includes(h))));
    map[key] = idx;
  });
  return map;
}

function parseNum(v){
  if(v === undefined || v === null || v === '') return 0;
  const n = parseFloat(String(v).replace(/[₹,$\s]/g, ''));
  return isNaN(n) ? 0 : n;
}

/* ============================================================
   3. FEATURE ENGINEERING (heuristic, dataset-adaptive)
   rowNums[i] is the 1-based data-row number of rows[i] (stable ID).
   ============================================================ */
function engineerFeatures(headers, rows, rowNums){
  const ci = findMLColIdx(headers);
  window._mlColMap = ci;

  const data = rows.map((row, i) => ({
    _rowNum:    rowNums ? rowNums[i] : i + 1,
    step:       ci.step >= 0       ? parseNum(row[ci.step])              : 0,
    type:       ci.type >= 0       ? String(row[ci.type] || '').toUpperCase().trim() : 'TRANSFER',
    amount:     ci.amount >= 0     ? parseNum(row[ci.amount])            : 0,
    nameOrig:   ci.nameOrig >= 0   ? String(row[ci.nameOrig] || '').trim() : '',
    nameDest:   ci.nameDest >= 0   ? String(row[ci.nameDest] || '').trim() : '',
    oldBalOrig: ci.oldBalOrig >= 0 ? parseNum(row[ci.oldBalOrig])        : 0,
    newBalOrig: ci.newBalOrig >= 0 ? parseNum(row[ci.newBalOrig])        : 0,
    oldBalDest: ci.oldBalDest >= 0 ? parseNum(row[ci.oldBalDest])        : 0,
    newBalDest: ci.newBalDest >= 0 ? parseNum(row[ci.newBalDest])        : 0,
    _raw: row
  }));

  // ---- Global aggregations ----
  const userCounts = {}, userSums = {}, userDests = {}, userAmounts = {};
  const destCounts = {}, destSenderMap = {};
  const allAmounts = data.map(d => d.amount).filter(a => a > 0);
  const globalMaxAmt = allAmounts.reduce((m, a) => a > m ? a : m, 1); // no spread: safe for large files

  data.forEach(d => {
    const orig = d.nameOrig || 'unknown';
    const dest = d.nameDest || 'unknown';
    userCounts[orig] = (userCounts[orig] || 0) + 1;
    userSums[orig]   = (userSums[orig] || 0) + d.amount;
    if(!userAmounts[orig]) userAmounts[orig] = [];
    userAmounts[orig].push(d.amount);
    if(!userDests[orig]) userDests[orig] = new Set();
    userDests[orig].add(dest);
    destCounts[dest] = (destCounts[dest] || 0) + 1;
    if(!destSenderMap[dest]) destSenderMap[dest] = new Set();
    destSenderMap[dest].add(orig);
  });

  // Benford — observed leading-digit distribution (descriptive; no statistical test yet)
  const benfordExpected = LS_CONFIG.benfordExpectedPct;
  const digitCounts = [0,0,0,0,0,0,0,0,0];
  data.forEach(d => {
    const a = Math.abs(d.amount);
    if(a >= 1){
      const fd = parseInt(String(Math.floor(a))[0], 10);
      if(fd >= 1 && fd <= 9) digitCounts[fd-1]++;
    }
  });
  const benfordN = digitCounts.reduce((s, c) => s + c, 0);
  const observedPct = digitCounts.map(c => benfordN ? (c / benfordN) * 100 : 0);
  const diffPP = observedPct.map((o, i) => o - benfordExpected[i]);
  window._mlBenford = {
    n: benfordN, counts: digitCounts, observedPct, expectedPct: benfordExpected.slice(), diffPP,
    overrepDigits: diffPP.map((d, i) => d > LS_CONFIG.benfordOverrepPP ? i + 1 : null).filter(Boolean)
  };

  // Sort by user + step for sequential analysis (ties broken by row number → deterministic)
  data.sort((a, b) =>
    a.nameOrig === b.nameOrig
      ? (a.step - b.step) || (a._rowNum - b._rowNum)
      : a.nameOrig.localeCompare(b.nameOrig)
  );

  let prevUser = '', prevStep = -999;

  return data.map(d => {
    const orig = d.nameOrig || 'unknown';
    const dest = d.nameDest || 'unknown';
    const txCount    = userCounts[orig] || 1;
    const avgAmount  = userSums[orig] / txCount;
    const isSameUser = (orig === prevUser);
    const timeGap    = isSameUser ? Math.max(d.step - prevStep, 0) : 999;

    const absAmt = Math.abs(d.amount);
    const fd = absAmt >= 1 ? parseInt(String(Math.floor(absAmt))[0], 10) : 0;
    const digitOverrep = (fd >= 1 && fd <= 9) ? window._mlBenford.diffPP[fd-1] > LS_CONFIG.benfordOverrepPP : false;

    const nameSim = stringSimilarity(orig, dest);
    const isSelfTransfer = (orig === dest) || nameSim > 0.72;

    const expectedNewBal = d.oldBalOrig - d.amount;
    const balanceError   = Math.abs(d.newBalOrig - expectedNewBal);

    const amtNorm      = d.amount / (globalMaxAmt + 1);
    const amtRatio     = d.amount / (d.oldBalOrig + 1);
    const amtDeviation = Math.abs(d.amount - avgAmount);

    const rapidFlag = isSameUser && timeGap <= 1 ? 1 : 0;

    const userPrev = (userAmounts[orig] || []).filter(a => a < d.amount);
    const userMax  = userPrev.length > 0 ? userPrev.reduce((m, a) => a > m ? a : m, 0) : avgAmount;
    const spikeFlag = (userMax > 0 && d.amount > 3 * userMax) ? 1 : 0;

    // 1-hop reciprocity only (dest has also paid orig). Not cycle detection.
    const circularFlag = destSenderMap[orig] && destSenderMap[orig].has(dest) ? 1 : 0;
    const manyToOneFlag = (destSenderMap[dest] ? destSenderMap[dest].size : 0) > 3 ? 1 : 0;

    // "Odd time": step % 24 used as an hour proxy. Only meaningful if step really is an hour.
    const hourProxy = d.step % 24;
    const oddTime   = (hourProxy >= 0 && hourProxy <= 5) ? 1 : 0;

    const features = {
      amount:              d.amount,
      amount_norm:         amtNorm,
      amount_ratio:        amtRatio,
      round_amount:        (d.amount > 0 && d.amount % 1000 === 0) ? 1 : 0,
      balance_error:       balanceError,
      balance_drain:       (d.oldBalOrig > 0 && d.newBalOrig < d.oldBalOrig * 0.1) ? 1 : 0,
      transaction_count:   txCount,
      avg_amount_user:     avgAmount,
      amount_deviation:    amtDeviation,
      is_new_dest:         (destCounts[dest] || 0) < 3 ? 1 : 0,
      time_bucket:         hourProxy,
      odd_time:            oddTime,
      time_gap:            timeGap,
      velocity:            1 / (timeGap + 1),
      dest_count:          destCounts[dest] || 0,
      new_vendor_risk:     (destCounts[dest] || 0) < 3 ? 1 : 0,
      name_similarity:     nameSim,
      fuzzy_flag:          nameSim > 0.72 ? 1 : 0,
      unique_receivers:    userDests[orig] ? userDests[orig].size : 1,
      multi_receiver_flag: (userDests[orig] ? userDests[orig].size : 1) > 4 ? 1 : 0,
      benford_flag:        digitOverrep ? 1 : 0,
      threshold_flag:      (d.amount > 4800 && d.amount < 5100) ? 1 : 0,
      sudden_spike:        spikeFlag,
      rapid_txn_flag:      rapidFlag,
      self_transfer:       isSelfTransfer ? 1 : 0,
      circular_flag:       circularFlag,
      many_to_one_flag:    manyToOneFlag,
      type_TRANSFER:       (d.type === 'TRANSFER') ? 1 : 0,
      type_CASH_OUT:       (d.type === 'CASH_OUT' || d.type === 'CASHOUT') ? 1 : 0,
      type_PAYMENT:        (d.type === 'PAYMENT') ? 1 : 0,
      type_DEBIT:          (d.type === 'DEBIT') ? 1 : 0,
    };

    prevUser = orig; prevStep = d.step;
    return { ...d, features };
  });
}

/* ============================================================
   4. HEURISTIC SCORER
   score = sigmoid(hand-set weighted sum) × 100  +  rule points,
   clamped to 0–100. The breakdown lists every component exactly
   (pre-clamp), so explanations are faithful to this formula.
   ============================================================ */
function sigmoid(x){ return 1 / (1 + Math.exp(-x)); }

// Hand-set weights — NOT learned from data, NOT calibrated.
const HEURISTIC_WEIGHTS = Object.freeze({
  amount_norm: 8.0, amount_ratio: 0.8, balance_error: 0.00002, balance_drain: 12.0,
  velocity: 4.0, odd_time: 10.0, new_vendor_risk: 4.0, fuzzy_flag: 14.0, self_transfer: 18.0,
  circular_flag: 16.0, many_to_one_flag: 10.0, multi_receiver_flag: 6.0, benford_flag: 6.0,
  threshold_flag: 7.0, sudden_spike: 9.0, rapid_txn_flag: 6.0, round_amount: 3.0,
  type_TRANSFER: 2.0, type_CASH_OUT: 6.0,
});
const HEURISTIC_BIAS = -8;

// Rule points. `key` identifies the rule for summaries; `text` is shown to the user.
function heuristicRules(f){
  return [
    { key:'self_transfer', pts:20, on: !!f.self_transfer, text:'Sender and receiver names identical or very similar (character overlap)' },
    { key:'reciprocal',    pts:18, on: !!f.circular_flag, text:'Receiver has also paid this sender (1-hop reciprocity; not full cycle detection)' },
    { key:'many_to_one',   pts:12, on: !!f.many_to_one_flag, text:'Receiver gets funds from more than 3 distinct senders' },
    { key:'balance_drain', pts:15, on: !!f.balance_drain, text:'Sender balance falls below 10% of opening balance' },
    { key:'odd_time',      pts:8,  on: !!f.odd_time, text:'Step value maps to hours 0–5 (step % 24; assumes step = hour)' },
    { key:'rapid',         pts:7,  on: !!f.rapid_txn_flag, text:'Another transaction by the same sender within 1 step' },
    { key:'spike',         pts:10, on: !!f.sudden_spike, text:'Amount above 3× the sender\'s other amounts' },
    { key:'fuzzy',         pts:8,  on: !!(f.fuzzy_flag && !f.self_transfer), text:'Sender/receiver names similar (character overlap)' },
    { key:'very_high_amt', pts:8,  on: f.amount > 500000, text:'Amount above 5,00,000 (' + formatAmtShort(f.amount) + ')' },
    { key:'high_amt',      pts:4,  on: f.amount > 50000 && f.amount <= 500000, text:'Amount above 50,000 (' + formatAmtShort(f.amount) + ')' },
    { key:'velocity',      pts:5,  on: f.velocity > 0.8, text:'Consecutive transactions by the same sender in the same step' },
    { key:'balance_error', pts:6,  on: f.balance_error > 1000, text:'Sender balance change differs from amount by more than 1,000' },
    { key:'round',         pts:4,  on: !!f.round_amount, text:'Amount is a multiple of 1,000' },
    { key:'benford_digit', pts:5,  on: !!f.benford_flag, text:'Leading digit over-represented vs Benford expectation in this file (descriptive, dataset-level)' },
    { key:'threshold',     pts:6,  on: !!f.threshold_flag, text:'Amount between 4,800 and 5,100 (fixed band; not a client-specific limit)' },
    { key:'fan_out',       pts:5,  on: !!f.multi_receiver_flag, text:'Sender paid more than 4 distinct receivers' },
    { key:'ratio',         pts:5,  on: f.amount_ratio > 5, text:'Amount exceeds 5× sender opening balance' },
    { key:'new_dest_large',pts:4,  on: !!(f.new_vendor_risk && f.amount > 10000), text:'Receiver seen fewer than 3 times and amount above 10,000' },
    { key:'cash_out',      pts:4,  on: !!f.type_CASH_OUT, text:'Transaction type is CASH_OUT' },
  ];
}

function tierForScore(score){
  if(score >= LS_CONFIG.thresholds.high) return 'high';
  if(score >= LS_CONFIG.thresholds.suspicious) return 'suspicious';
  return 'safe';
}
const TIER_LABEL  = { high:'High risk', suspicious:'Suspicious', safe:'Below thresholds' };
const TIER_ACTION = {
  high: 'Prioritise for manual vouching (heuristic preview — not a fraud determination)',
  suspicious: 'Consider for review sample',
  safe: 'No heuristic rule exceeded the thresholds',
};

function predictTransaction(f){
  let z = HEURISTIC_BIAS;
  Object.entries(HEURISTIC_WEIGHTS).forEach(([k, w]) => { z += (f[k] || 0) * w; });
  const basePts = sigmoid(z) * 100;

  const components = [{ key:'weighted_features', text:'Weighted feature score (sigmoid of hand-set weights)', pts: basePts }];
  heuristicRules(f).forEach(r => { if(r.on) components.push({ key:r.key, text:r.text, pts:r.pts }); });

  const rawTotal  = components.reduce((s, c) => s + c.pts, 0);
  const riskScore = Math.min(Math.max(Math.round(rawTotal), 0), 100);
  const tier      = tierForScore(riskScore);
  const reasons   = components.slice(1).map(c => c.text);
  if(reasons.length === 0) reasons.push('No heuristic rule fired');

  return { riskScore, rawTotal, tier, decision: TIER_LABEL[tier], action: TIER_ACTION[tier], reasons, components, features: f };
}

/* ============================================================
   5. RULE CONTRIBUTION SUMMARY (exact for this heuristic; not SHAP)
   Share of total pre-cap points contributed by each component,
   summed over a set of scored rows.
   ============================================================ */
function ruleContributionSummary(rows){
  const totals = {}, labels = {}, fired = {};
  let grand = 0;
  rows.forEach(r => r.mlResult.components.forEach(c => {
    totals[c.key] = (totals[c.key] || 0) + c.pts;
    fired[c.key]  = (fired[c.key] || 0) + 1;
    labels[c.key] = c.key === 'weighted_features' ? c.text : c.text.replace(/\s*\(₹[^)]*\)$/, '');
    grand += c.pts;
  }));
  return Object.keys(totals)
    .map(k => ({ key:k, label:labels[k], pct: grand ? (totals[k] / grand) * 100 : 0, rowsFired: fired[k] }))
    .sort((a, b) => b.pct - a.pct);
}

/* ============================================================
   6. MAIN RUNNER — called by launchAudit()
   ============================================================ */
function runMLOnData(){
  if(_csvRawHeaders.length === 0 || _csvRawRows.length === 0) return;

  const rowNums = _csvRawRows.map((_, i) => i + 1);
  const enriched = engineerFeatures(_csvRawHeaders, _csvRawRows, rowNums);

  window._mlResults = enriched.map(d => ({ ...d, mlResult: predictTransaction(d.features) }));

  const m = autoMapIdx();
  tableData.length = 0;
  window._mlResults.forEach((d, idx) => {
    const entry = rowToEntry(d._raw, m, d._rowNum);
    entry.r = d.mlResult.tier;
    entry.rsn = d.mlResult.reasons.slice(0, 3).join(' | ');
    entry._mlIdx   = idx;
    entry._mlScore = d.mlResult.riskScore;
    if(!entry.v || entry.v === 'Unknown') entry.v = d.nameDest || 'Unknown';
    d._displayId = entry.id;
    tableData.push(entry);
  });

  const R = window._mlResults;
  const high  = R.filter(d => d.mlResult.tier === 'high').length;
  const susp  = R.filter(d => d.mlResult.tier === 'suspicious').length;
  const safe  = R.filter(d => d.mlResult.tier === 'safe').length;
  const total = R.length;
  const avgRisk = R.reduce((s, d) => s + d.mlResult.riskScore, 0) / (total || 1);
  const highAmt = R.filter(d => d.mlResult.tier === 'high').reduce((s, d) => s + (d.amount || 0), 0);
  const suspAmt = R.filter(d => d.mlResult.tier === 'suspicious').reduce((s, d) => s + (d.amount || 0), 0);
  const flaggedPct = total ? ((high + susp) / total) * 100 : 0;

  window._mlSummary = {
    high, susp, safe, total,
    avgRisk: Math.round(avgRisk),
    totalFraudAmt: highAmt,      // kept for compatibility: = amount in high-risk rows
    highAmt, suspAmt,
    flaggedPct,
    calibrationWarning: flaggedPct > LS_CONFIG.reviewBudgetPct,
    mapped: window._mlColMap || {},
  };

  renderT();
  window._scatterBuilt = false;
  window._barsAnimated = false;
}

/* ============================================================
   7. DASHBOARD UPDATERS (all values computed; text via textContent)
   ============================================================ */
function pct(n, total){ return total ? (n / total) * 100 : 0; }

function updateDashboardWithML(){
  if(!window._mlSummary) return;
  const s = window._mlSummary;

  // Hero panel
  setText('hero-n-high', s.high.toLocaleString());
  setText('hero-n-susp', s.susp.toLocaleString());
  setText('hero-n-safe', s.safe.toLocaleString());
  setText('hero-pct-high', pct(s.high, s.total).toFixed(1) + '%');
  setText('hero-pct-susp', pct(s.susp, s.total).toFixed(1) + '%');

  const circ = 2 * Math.PI * 54;
  const gaugeRing = document.getElementById('fg-ring');
  if(gaugeRing) gaugeRing.style.strokeDashoffset = circ - (circ * s.avgRisk / 100);
  setText('fg-n', String(s.avgRisk));
  const loader = document.getElementById('mini-loader');
  if(loader) loader.style.width = Math.min(100, s.flaggedPct).toFixed(1) + '%';

  setText('hero-findings',
    'Heuristic preview flagged ' + s.high.toLocaleString() + ' high-risk (' + formatAmtShort(s.highAmt) + ') and ' +
    s.susp.toLocaleString() + ' suspicious rows out of ' + s.total.toLocaleString() +
    ' (' + s.flaggedPct.toFixed(1) + '% of rows). These are rule-based indicators for review, not fraud findings.');

  // Heuristic / calibration notice (A8)
  const notice = document.getElementById('heuristic-notice');
  if(notice){
    notice.style.display = 'block';
    notice.classList.toggle('warn', s.calibrationWarning);
    setText('heuristic-notice-text',
      LS_CONFIG.engineName + '. Flag rate: ' + s.flaggedPct.toFixed(1) + '% of rows' +
      (s.calibrationWarning
        ? ' — above the ' + LS_CONFIG.reviewBudgetPct + '% review budget, so this preview is NOT calibrated for this file (known issue; replaced in roadmap P9).'
        : '.'));
  }

  updateOverviewTab();
  updateTrendsTab();
  updateRelationshipsTab();
  updateExplainabilityTab();
  updateInsightsAndMemo();
}

/* ── Overview Tab ── */
function updateOverviewTab(){
  const s = window._mlSummary; if(!s) return;
  const total = s.total || 1;

  setText('ov-total', 'Total ' + s.total.toLocaleString() + ' TX');
  setText('donut-total', s.total >= 1000 ? (s.total / 1000).toFixed(1) + 'K' : String(s.total));
  setText('leg-high', '■ High ' + pct(s.high, total).toFixed(1) + '% (' + s.high.toLocaleString() + ')');
  setText('leg-susp', '■ Susp ' + pct(s.susp, total).toFixed(1) + '% (' + s.susp.toLocaleString() + ')');
  setText('leg-safe', '■ Below ' + pct(s.safe, total).toFixed(1) + '% (' + s.safe.toLocaleString() + ')');

  // Donut: segments start at 12 o'clock (circles are rotated −90°), in order high → susp → safe.
  const C = 2 * Math.PI * 80;
  let offset = 0;
  [['donut-high', s.high], ['donut-susp', s.susp], ['donut-safe', s.safe]].forEach(([id, n]) => {
    const seg = document.getElementById(id);
    const len = C * (n / total);
    if(seg){
      seg.setAttribute('stroke-dasharray', len.toFixed(3) + ' ' + (C - len).toFixed(3));
      seg.setAttribute('stroke-dashoffset', (-offset).toFixed(3));
      seg.setAttribute('data-count', String(n));
    }
    offset += len;
  });

  // Quick stats (all computed)
  setText('qs-avg-high', s.high ? formatAmtShort(s.highAmt / s.high) : '—');
  setText('qs-high-amt', formatAmtShort(s.highAmt));
  setText('qs-flag-rate', s.flaggedPct.toFixed(1) + '%');
  const highPayees = new Set(window._mlResults.filter(d => d.mlResult.tier === 'high').map(d => d.nameDest || 'Unknown'));
  setText('qs-payees', highPayees.size.toLocaleString());

  // Exposure card
  setText('exp-val', formatAmtShort(s.highAmt));
  setText('exp-desc', 'Sum of amounts in ' + s.high.toLocaleString() + ' high-risk rows (+' + formatAmtShort(s.suspAmt) + ' in ' + s.susp.toLocaleString() + ' suspicious rows).');

  // Tier bars
  [['high', s.high], ['susp', s.susp], ['safe', s.safe]].forEach(([k, n]) => {
    setText('tier-' + k + '-txt', n.toLocaleString() + ' TX');
    setText('tier-' + k + '-pct', pct(n, total).toFixed(1) + '%');
    const bar = document.getElementById('tier-' + k + '-bar');
    if(bar) bar.style.width = pct(n, total).toFixed(1) + '%';
  });
}

/* ── Trends Tab ── */
function updateTrendsTab(){
  const results = window._mlResults;
  if(!results || results.length === 0) return;
  const barsContainer = document.getElementById('trends-monthly-bars');
  const alertMsg = document.getElementById('trends-monthly-alert');
  const hasStep = window._mlSummary.mapped && window._mlSummary.mapped.step >= 0;

  if(!hasStep){
    setChildren(barsContainer, el('div', { class:'na-inline' }, 'Not available — no time/step column was mapped in this file.'));
    if(alertMsg) alertMsg.textContent = 'Time-based trends need a date or step column. Real calendar dates are supported from roadmap P3/P6.';
  } else {
    // Bucket by the actual step values (≤ 12 equal-width buckets). No invented calendar months.
    let minS = Infinity, maxS = -Infinity;
    results.forEach(d => { if(d.step < minS) minS = d.step; if(d.step > maxS) maxS = d.step; });
    const span = maxS - minS + 1;
    const nb = Math.max(1, Math.min(12, Math.ceil(span)));
    const width = span / nb;
    const bins = Array.from({ length: nb }, (_, i) => ({ lo: Math.ceil(minS + i * width), hi: Math.ceil(minS + (i + 1) * width) - 1, safe:0, susp:0, high:0 }));
    results.forEach(d => {
      let b = Math.floor((d.step - minS) / width); if(b >= nb) b = nb - 1; if(b < 0) b = 0;
      bins[b][d.mlResult.tier === 'high' ? 'high' : d.mlResult.tier === 'suspicious' ? 'susp' : 'safe']++;
    });
    const maxCol = bins.reduce((m, b) => Math.max(m, b.safe + b.susp + b.high), 1);
    const meanHigh = bins.reduce((a, b) => a + b.high, 0) / nb;

    const cols = bins.map(b => {
      const isSpike = b.high > meanHigh * 3 && b.high > 2;
      const stack = el('div', { style:'width:100%;flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px' }, [
        b.high ? el('div', { style:'height:' + (b.high / maxCol * 190).toFixed(1) + 'px;background:var(--red);border-radius:2px 2px 0 0', title: b.high + ' high-risk' }) : null,
        b.susp ? el('div', { style:'height:' + (b.susp / maxCol * 190).toFixed(1) + 'px;background:var(--amber)', title: b.susp + ' suspicious' }) : null,
        b.safe ? el('div', { style:'height:' + Math.max(2, b.safe / maxCol * 190).toFixed(1) + 'px;background:var(--cyan);opacity:0.65', title: b.safe + ' below thresholds' }) : null,
      ]);
      const label = el('div', { style:'font-family:var(--mono);font-size:0.58rem;margin-top:5px;color:' + (isSpike ? 'var(--red)' : 'var(--txt3)') },
        b.lo >= b.hi ? 'step ' + b.lo : 'step ' + b.lo + '–' + b.hi);
      return el('div', { style:'flex:1;display:flex;flex-direction:column;align-items:center;height:100%;position:relative' }, [
        isSpike ? el('div', { style:'position:absolute;top:-22px;background:var(--red);color:#fff;font-family:var(--mono);font-size:0.52rem;padding:2px 5px;border-radius:4px' }, '▲ >3× avg') : null,
        stack, label ]);
    });
    setChildren(barsContainer, cols);
    const spikes = bins.filter(b => b.high > meanHigh * 3 && b.high > 2).length;
    if(alertMsg) alertMsg.textContent = spikes
      ? spikes + ' step bucket(s) hold more than 3× the average number of high-risk rows. Buckets use the file\'s step values, not calendar months.'
      : 'No step bucket holds more than 3× the average number of high-risk rows. Buckets use the file\'s step values, not calendar months.';
  }

  renderBenfordChart();

  // Top payees by flagged amount
  const exposure = {};
  let totalExposure = 0;
  results.filter(d => d.mlResult.tier !== 'safe').forEach(d => {
    const v = d.nameDest || 'Unknown';
    if(!exposure[v]) exposure[v] = { high:0, susp:0, total:0 };
    exposure[v].total += d.amount || 0;
    exposure[v][d.mlResult.tier === 'high' ? 'high' : 'susp'] += d.amount || 0;
    totalExposure += d.amount || 0;
  });
  const sorted = Object.entries(exposure).sort((a, b) => b[1].total - a[1].total).slice(0, 6);
  const vendorsContainer = document.getElementById('trends-top-vendors');
  if(vendorsContainer){
    if(sorted.length === 0){
      setChildren(vendorsContainer, el('div', { style:'color:var(--txt3);font-size:0.85rem' }, 'No rows above the heuristic thresholds.'));
    } else {
      const maxExp = sorted[0][1].total || 1;
      setChildren(vendorsContainer, sorted.map(([vendor, exp]) => {
        const isHigh = exp.high > exp.susp;
        return el('div', null, [
          el('div', { style:'display:flex;justify-content:space-between;margin-bottom:5px' }, [
            el('span', { style:'font-size:0.85rem' }, [vendor + ' ',
              el('span', { style:'font-family:var(--mono);font-size:0.6rem;padding:1px 6px;border-radius:3px;margin-left:6px;background:' + (isHigh ? 'var(--red-dim);color:var(--red)' : 'var(--amber-dim);color:var(--amber)') }, isHigh ? 'HIGH' : 'SUSP')]),
            el('span', { style:'font-family:var(--mono);font-size:0.78rem;color:' + (isHigh ? 'var(--red)' : 'var(--amber)') }, formatAmtShort(exp.total)),
          ]),
          el('div', { style:'height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;display:flex' }, [
            exp.high ? el('div', { style:'width:' + (exp.high / maxExp * 100).toFixed(1) + '%;height:100%;background:var(--red)' }) : null,
            exp.susp ? el('div', { style:'width:' + (exp.susp / maxExp * 100).toFixed(1) + '%;height:100%;background:var(--amber)' }) : null,
          ]),
        ]);
      }));
    }
  }
  setText('trends-total-exposure', formatAmtShort(totalExposure));
  setText('trends-exposure-desc', sorted.length
    ? 'Sum of high-risk + suspicious amounts. Largest share: ' + sorted[0][0] + '.'
    : 'No rows above the heuristic thresholds.');
}

/* Benford chart — rendered from computed digit counts. No significance test yet (P7). */
function renderBenfordChart(){
  const b = window._mlBenford;
  const box = document.getElementById('benford-bars');
  if(!box || !b) return;
  if(b.n === 0){
    setChildren(box, el('div', { class:'na-inline' }, 'Not available — no positive amounts found.'));
    return;
  }
  const maxPct = Math.max(...b.observedPct, ...b.expectedPct, 1);
  setChildren(box, b.observedPct.map((o, i) => {
    const e = b.expectedPct[i];
    const over = b.diffPP[i] > LS_CONFIG.benfordOverrepPP;
    return el('div', { style:'flex:1;display:flex;flex-direction:column;align-items:center' }, [
      el('div', { style:'width:100%;display:flex;flex-direction:row;align-items:flex-end;justify-content:center;height:130px;gap:2px' }, [
        el('div', { style:'width:45%;height:' + (e / maxPct * 128).toFixed(1) + 'px;background:var(--cyan);opacity:0.25;border-radius:2px 2px 0 0', title:'Expected ' + e.toFixed(1) + '%' }),
        el('div', { style:'width:45%;height:' + (o / maxPct * 128).toFixed(1) + 'px;background:' + (over ? 'var(--red)' : 'var(--cyan)') + ';border-radius:2px 2px 0 0', title:'Observed ' + o.toFixed(1) + '% (' + b.counts[i] + ')' }),
      ]),
      el('div', { style:'font-family:var(--mono);font-size:0.58rem;color:' + (over ? 'var(--red)' : 'var(--txt3)') }, String(i + 1)),
    ]);
  }));
  const big = b.diffPP.map((d, i) => ({ d, digit: i + 1 })).sort((x, y) => Math.abs(y.d) - Math.abs(x.d))[0];
  setText('benford-note',
    'n = ' + b.n.toLocaleString() + ' amounts ≥ 1. Largest gap: digit ' + big.digit + ' observed ' +
    b.observedPct[big.digit - 1].toFixed(1) + '% vs expected ' + b.expectedPct[big.digit - 1].toFixed(1) + '% (' +
    (big.d >= 0 ? '+' : '') + big.d.toFixed(1) + ' pp). Red = more than ' + LS_CONFIG.benfordOverrepPP +
    ' pp above expectation. Descriptive only: no statistical test is applied yet (roadmap P7), and deviation is not evidence of fabrication.');
}

/* ── Explainability Tab ── */
function updateExplainabilityTab(){
  const results = window._mlResults;
  if(!results || results.length === 0) return;
  const flagged = results.filter(d => d.mlResult.tier !== 'safe');
  const exBars = document.getElementById('ex-bars');
  setText('ex-risk-label', 'Avg score = ' + window._mlSummary.avgRisk + '/100');
  if(flagged.length === 0){
    setText('ex-desc', 'No rows exceeded the heuristic thresholds, so there is nothing to break down.');
    setChildren(exBars, []);
    return;
  }
  setText('ex-desc',
    'Share of the heuristic score points contributed by each component, summed over the ' +
    flagged.length.toLocaleString() + ' high-risk + suspicious rows (pre-cap points). This is an exact breakdown of the ' +
    'rule-based preview formula — it is not SHAP and not a trained-model attribution. Per-row breakdowns are in the transaction table.');
  const rows = ruleContributionSummary(flagged).slice(0, 8);
  setChildren(exBars, rows.map((c, i) => {
    const w = Math.max(1, Math.round(c.pct));
    return el('div', { class:'fac-row', title: c.label + ' — fired in ' + c.rowsFired.toLocaleString() + ' flagged rows' }, [
      el('span', { class:'fac-label' }, c.label),
      el('div', { class:'fac-bar-bg' }, el('div', { class:'fac-bar-fill', style:'background:' + (i < 3 ? 'var(--red)' : i < 5 ? 'var(--amber)' : 'var(--cyan)') + ';width:' + w + '%', 'data-w': String(w) })),
      el('span', { class:'fac-val' }, c.pct.toFixed(0) + '%'),
    ]);
  }));
}

/* ── Relationships Tab (computed counts only; graph analysis arrives in P12–P13) ── */
function updateRelationshipsTab(){
  const R = window._mlResults;
  if(!R || R.length === 0) return;
  const byPayee = {};
  R.filter(d => d.mlResult.tier === 'high').forEach(d => {
    const v = d.nameDest || 'Unknown';
    if(!byPayee[v]) byPayee[v] = { name:v, amt:0, count:0 };
    byPayee[v].amt += d.amount || 0;
    byPayee[v].count++;
  });
  const sorted = Object.values(byPayee).sort((a, b) => b.amt - a.amt);
  const recip = R.filter(d => d.features.circular_flag === 1).length;
  const fanInPayees = new Set(R.filter(d => d.features.many_to_one_flag === 1).map(d => d.nameDest || 'Unknown')).size;

  setText('rel-stat-payees', sorted.length.toLocaleString());
  setText('rel-stat-amount', formatAmtShort(sorted.reduce((s, v) => s + v.amt, 0)));
  setText('rel-stat-recip', recip.toLocaleString());
  setText('rel-stat-fanin', fanInPayees.toLocaleString());

  const cards = document.getElementById('re-entity-cards');
  if(cards){
    setChildren(cards, sorted.length === 0
      ? [el('div', { class:'na-inline', style:'grid-column:1/-1' }, 'No high-risk rows in this file.')]
      : sorted.slice(0, 5).map(v => el('div', { style:'background:var(--bg3);border-radius:10px;padding:14px;border-top:2px solid var(--red)' }, [
          el('div', { style:'font-family:var(--mono);font-size:0.7rem;color:var(--red);margin-bottom:8px;font-weight:600;word-break:break-all' }, v.name),
          el('div', { style:'font-size:0.72rem;color:var(--txt3);margin-bottom:4px' }, formatAmtShort(v.amt) + ' in high-risk rows'),
          el('div', { style:'font-size:0.72rem;color:var(--txt3);margin-bottom:8px' }, v.count.toLocaleString() + ' high-risk transaction' + (v.count === 1 ? '' : 's')),
          el('span', { style:'background:var(--red-dim);color:var(--red);font-family:var(--mono);font-size:0.58rem;padding:2px 6px;border-radius:3px' }, 'HEURISTIC FLAG'),
        ])));
  }
}

/* ── Insights cards + memo (computed sentences only) ── */
function updateInsightsAndMemo(){
  const s = window._mlSummary; if(!s) return;
  const R = window._mlResults;
  const highRows = R.filter(d => d.mlResult.tier === 'high');
  const highPayees = new Set(highRows.map(d => d.nameDest || 'Unknown'));
  const topRules = ruleContributionSummary(highRows).filter(c => c.key !== 'weighted_features').sort((a, b) => b.rowsFired - a.rowsFired).slice(0, 3);

  setText('insights-total-fraud', formatAmtShort(s.highAmt));
  setText('insights-vendors', highPayees.size.toLocaleString() + ' payees');
  setText('insights-pattern', topRules.length ? topRules[0].label : 'None (no high-risk rows)');

  const doc = document.getElementById('print-doc');
  if(!doc) return;
  const fileName = (typeof _csvFileName !== 'undefined') ? _csvFileName : 'uploaded file';
  const mappedCols = Object.entries(s.mapped || {}).filter(([, i]) => i >= 0).map(([k, i]) => k + ' → "' + _csvRawHeaders[i] + '"');
  const ignoredLabels = _csvRawHeaders.filter(h => lsIsLabelColumn(h));

  const sec = (title, body) => [el('h3', null, title)].concat(body);
  const p = t => el('p', null, t);
  const ul = items => el('ul', null, items.map(t => el('li', null, t)));

  setChildren(doc, [].concat(
    [el('h1', null, 'Preliminary Analysis Memo'),
     el('p', { style:'text-align:center;font-family:var(--mono);font-size:0.7rem;margin-bottom:32px;color:var(--txt3)' },
       'HEURISTIC PREVIEW • GENERATED IN-BROWSER FROM THE LOADED FILE • NOT A FINAL AUDIT OPINION')],
    sec('1. Scope', [ul([
      'File: ' + fileName + ' — ' + s.total.toLocaleString() + ' data rows analysed.',
      'Columns used by the preview: ' + (mappedCols.length ? mappedCols.join('; ') : 'none detected') + '.',
      ignoredLabels.length ? 'Label column(s) present but excluded from scoring: ' + ignoredLabels.join(', ') + '.' : 'No label columns detected.',
    ])]),
    sec('2. Results of the heuristic preview', [ul([
      s.high.toLocaleString() + ' rows (' + pct(s.high, s.total).toFixed(1) + '%) scored ≥ ' + LS_CONFIG.thresholds.high + ' (high risk), totalling ' + formatAmtShort(s.highAmt) + ' across ' + highPayees.size.toLocaleString() + ' payees.',
      s.susp.toLocaleString() + ' rows (' + pct(s.susp, s.total).toFixed(1) + '%) scored ' + LS_CONFIG.thresholds.suspicious + '–' + (LS_CONFIG.thresholds.high - 1) + ' (suspicious), totalling ' + formatAmtShort(s.suspAmt) + '.',
      'Average heuristic score: ' + s.avgRisk + '/100.',
    ])]),
    sec('3. Most frequent indicators among high-risk rows', [topRules.length
      ? ul(topRules.map(r => r.label + ' — ' + r.rowsFired.toLocaleString() + ' of ' + highRows.length.toLocaleString() + ' rows.'))
      : p('No high-risk rows.')]),
    sec('4. Limitations', [ul([
      'Scores come from hand-set rules and weights, not a trained or calibrated model.',
      s.calibrationWarning
        ? 'The flag rate (' + s.flaggedPct.toFixed(1) + '%) exceeds the ' + LS_CONFIG.reviewBudgetPct + '% review budget; results are not calibrated for this file.'
        : 'Flag rate: ' + s.flaggedPct.toFixed(1) + '% of rows.',
      'Not yet performed: bank statement reconciliation, entity resolution, relationship/graph analysis, statistical Benford testing, industry benchmarking, going-concern simulation.',
    ])]),
    sec('5. Suggested next step', [p('Select a sample of the high-risk rows above for manual vouching against source documents. Do not treat any row as fraudulent on the basis of this preview alone.')])
  ));

  const copyBtn = document.getElementById('memo-copy-btn');
  if(copyBtn) copyBtn.disabled = false;
}

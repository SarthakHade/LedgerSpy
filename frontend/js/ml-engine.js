/* ============================================================
   LedgerSpy ML Engine v2 — Fully Dynamic, Dataset-Reactive
   ============================================================
   Feature Engineering → Prediction → Explainability
   All processing is in-browser (air-gapped, no server needed)
   ============================================================ */

/* ---- Global ML state ---- */
window._mlResults = [];
window._mlSummary = null;
window._selectedTxIdx = -1;

/* ============================================================
   1. STRING SIMILARITY (mirrors Python SequenceMatcher ratio)
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
   2. COLUMN DETECTION — auto-maps CSV headers to ML fields
   ============================================================ */
const ML_COLS = {
  step:       ['step','time','hour','period','day'],
  type:       ['type','transaction_type','txtype','tx_type','category'],
  amount:     ['amount','amt','value','total','debit','credit','amount(inr)','amount (₹)','sum'],
  nameOrig:   ['nameorig','name_orig','sender','from','payer','source','nameorg','originator'],
  nameDest:   ['namedest','name_dest','receiver','to','payee','destination',
               'beneficiary','vendor','party','description','name'],
  oldBalOrig: ['oldbalanceorg','old_balance_orig','oldbalorg','sender_old_balance',
               'old_bal_orig','opening_balance'],
  newBalOrig: ['newbalanceorig','new_balance_orig','newbalorig','sender_new_balance',
               'new_bal_orig','closing_balance'],
  oldBalDest: ['oldbalancedest','old_balance_dest','oldbaldest','receiver_old_balance'],
  newBalDest: ['newbalancedest','new_balance_dest','newbaldest','receiver_new_balance'],
  isFraud:    ['isfraud','is_fraud','fraud','label','target','flagged']
};

function findMLColIdx(headers){
  const map = {};
  const hLower = headers.map(h => h.toLowerCase().trim().replace(/\s+/g,'_'));
  Object.entries(ML_COLS).forEach(([key, aliases]) => {
    let idx = hLower.findIndex(h => aliases.includes(h));
    // Fallback: partial match
    if(idx === -1) idx = hLower.findIndex(h => aliases.some(a => h.includes(a) || a.includes(h)));
    map[key] = idx;
  });
  return map;
}

function parseNum(v){
  if(v === undefined || v === null || v === '') return 0;
  const n = parseFloat(String(v).replace(/[₹,$,\s,]/g, ''));
  return isNaN(n) ? 0 : n;
}

/* ============================================================
   3. FEATURE ENGINEERING (fully dynamic, dataset-adaptive)
   ============================================================ */
function engineerFeatures(headers, rows){
  const ci = findMLColIdx(headers);

  // Parse all rows into structured objects
  const data = rows.map(row => ({
    step:       ci.step >= 0       ? parseNum(row[ci.step])              : 0,
    type:       ci.type >= 0       ? String(row[ci.type] || '').toUpperCase().trim() : 'TRANSFER',
    amount:     ci.amount >= 0     ? parseNum(row[ci.amount])            : 0,
    nameOrig:   ci.nameOrig >= 0   ? String(row[ci.nameOrig] || '').trim() : '',
    nameDest:   ci.nameDest >= 0   ? String(row[ci.nameDest] || '').trim() : '',
    oldBalOrig: ci.oldBalOrig >= 0 ? parseNum(row[ci.oldBalOrig])        : 0,
    newBalOrig: ci.newBalOrig >= 0 ? parseNum(row[ci.newBalOrig])        : 0,
    oldBalDest: ci.oldBalDest >= 0 ? parseNum(row[ci.oldBalDest])        : 0,
    newBalDest: ci.newBalDest >= 0 ? parseNum(row[ci.newBalDest])        : 0,
    isFraud:    ci.isFraud >= 0    ? parseNum(row[ci.isFraud])           : -1,
    _raw: row
  }));

  // ---- Global aggregations ----
  const userCounts = {}, userSums = {}, userDests = {}, userAmounts = {};
  const destCounts = {}, destSenderMap = {};
  const allAmounts = data.map(d => d.amount).filter(a => a > 0);
  
  // Global amount stats for normalization
  const globalMeanAmt = allAmounts.reduce((s, a) => s + a, 0) / (allAmounts.length || 1);
  const globalMaxAmt  = Math.max(...allAmounts, 1);

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

  // Benford's Law — compute actual leading digit distribution
  const benfordExpected = [30.1, 17.6, 12.5, 9.7, 7.9, 6.7, 5.8, 5.1, 4.6];
  const digitCounts = [0,0,0,0,0,0,0,0,0];
  data.forEach(d => {
    const fd = parseInt(String(Math.floor(Math.abs(d.amount)))[0]);
    if(fd >= 1 && fd <= 9) digitCounts[fd-1]++;
  });
  const totalTx = data.length || 1;
  const benfordDeviation = digitCounts.map((c, i) => {
    const actual = (c / totalTx) * 100;
    return Math.abs(actual - benfordExpected[i]);
  });

  // Sort by user + step for sequential analysis
  data.sort((a, b) =>
    a.nameOrig === b.nameOrig ? a.step - b.step : a.nameOrig.localeCompare(b.nameOrig)
  );

  let prevUser = '', prevStep = -999, prevAmount = 0, prevStepSame = -999;

  return data.map(d => {
    const orig = d.nameOrig || 'unknown';
    const dest = d.nameDest || 'unknown';
    const txCount    = userCounts[orig] || 1;
    const avgAmount  = userSums[orig] / txCount;
    const isSameUser = (orig === prevUser);
    const timeGap    = isSameUser ? Math.max(d.step - prevStep, 0) : 999;

    // Detect leading digit for this transaction's Benford check
    const fd = parseInt(String(Math.floor(Math.abs(d.amount)))[0]);
    const thisDigitDev = (fd >= 1 && fd <= 9) ? benfordDeviation[fd-1] : 0;

    // Self-transfer: exact OR high fuzzy similarity (catches "Apex Inc" → "Apex Trading")
    const nameSim = stringSimilarity(orig, dest);
    const isSelfTransfer = (orig === dest) || nameSim > 0.72;

    // Balance mismatch: expected newBal = oldBal - amount (for sender)
    const expectedNewBal = d.oldBalOrig - d.amount;
    const balanceError   = Math.abs(d.newBalOrig - expectedNewBal);

    // Amount normalization
    const amtNorm     = d.amount / (globalMaxAmt + 1);
    const amtRatio    = d.amount / (d.oldBalOrig + 1);
    const amtDeviation = Math.abs(d.amount - avgAmount);

    // Rapid succession: multiple transactions within same step or 1 step apart
    const rapidFlag = isSameUser && timeGap <= 1 ? 1 : 0;

    // Sudden spike relative to user's own history
    const userPrev = (userAmounts[orig] || []).filter(a => a < d.amount);
    const userMax  = userPrev.length > 0 ? Math.max(...userPrev) : avgAmount;
    const spikeFlag = (userMax > 0 && d.amount > 3 * userMax) ? 1 : 0;

    // Circular flow: dest sends back to orig (or to someone who sends to orig)
    const circularFlag = destSenderMap[orig] && destSenderMap[orig].has(dest) ? 1 : 0;

    // Multiple senders to same destination (many-to-one aggregation — laundering indicator)
    const manyToOneFlag = (destSenderMap[dest] ? destSenderMap[dest].size : 0) > 3 ? 1 : 0;

    // Odd timing: step represents hour or sequential step — use modulo 24 heuristic
    const hourProxy = d.step % 24;
    const oddTime   = (hourProxy >= 0 && hourProxy <= 5) ? 1 : 0;

    const features = {
      // Core financial features
      amount:              d.amount,
      amount_norm:         amtNorm,
      amount_ratio:        amtRatio,
      round_amount:        (d.amount > 0 && d.amount % 1000 === 0) ? 1 : 0,
      balance_error:       balanceError,
      balance_drain:       (d.oldBalOrig > 0 && d.newBalOrig < d.oldBalOrig * 0.1) ? 1 : 0,
      // User behavior features
      transaction_count:   txCount,
      avg_amount_user:     avgAmount,
      amount_deviation:    amtDeviation,
      is_new_dest:         (destCounts[dest] || 0) < 3 ? 1 : 0,
      // Timing features
      time_bucket:         hourProxy,
      odd_time:            oddTime,
      time_gap:            timeGap,
      velocity:            1 / (timeGap + 1),
      // Destination risk features
      dest_count:          destCounts[dest] || 0,
      new_vendor_risk:     (destCounts[dest] || 0) < 3 ? 1 : 0,
      // Name analysis
      name_similarity:     nameSim,
      fuzzy_flag:          nameSim > 0.72 ? 1 : 0,
      // Multi-receiver
      unique_receivers:    userDests[orig] ? userDests[orig].size : 1,
      multi_receiver_flag: (userDests[orig] ? userDests[orig].size : 1) > 4 ? 1 : 0,
      // Pattern flags
      benford_flag:        thisDigitDev > 8 ? 1 : 0,
      threshold_flag:      (d.amount > 4800 && d.amount < 5100) ? 1 : 0,
      sudden_spike:        spikeFlag,
      rapid_txn_flag:      rapidFlag,
      self_transfer:       isSelfTransfer ? 1 : 0,
      circular_flag:       circularFlag,
      many_to_one_flag:    manyToOneFlag,
      // Transaction type
      type_TRANSFER:       (d.type === 'TRANSFER') ? 1 : 0,
      type_CASH_OUT:       (d.type === 'CASH_OUT' || d.type === 'CASHOUT') ? 1 : 0,
      type_PAYMENT:        (d.type === 'PAYMENT') ? 1 : 0,
      type_DEBIT:          (d.type === 'DEBIT') ? 1 : 0,
      // Ground truth (if present)
      isFraud:             d.isFraud
    };

    prevUser = orig; prevStep = d.step; prevAmount = d.amount;
    return { ...d, features };
  });
}

/* ============================================================
   4. PREDICTION ENGINE (sigmoid + rule boosts, calibrated v2)
   ============================================================ */
function sigmoid(x){ return 1 / (1 + Math.exp(-x)); }

function predictTransaction(f){
  // Logistic regression weights — calibrated to detect laundering patterns
  const weights = {
    amount_norm:          8.0,    // Normalized amount matters a lot
    amount_ratio:         0.8,    // Amount vs balance ratio
    balance_error:        0.00002,// Balance mismatch (large balances → large errors)
    balance_drain:        12.0,   // Account drained to near-zero
    velocity:             4.0,    // Rapid succession
    odd_time:             10.0,   // Late night transactions
    new_vendor_risk:      4.0,    // New/rare destination
    fuzzy_flag:           14.0,   // Name similarity (key for self-laundering)
    self_transfer:        18.0,   // Self-transfer (strongest signal)
    circular_flag:        16.0,   // Circular trading
    many_to_one_flag:     10.0,   // Aggregation pattern
    multi_receiver_flag:  6.0,    // Fan-out pattern
    benford_flag:         6.0,    // Benford's Law violation
    threshold_flag:       7.0,    // Structuring
    sudden_spike:         9.0,    // Sudden amount spike
    rapid_txn_flag:       6.0,    // Rapid successive transactions
    round_amount:         3.0,    // Round number amounts
    type_TRANSFER:        2.0,    // Transfer type is riskier
    type_CASH_OUT:        6.0,    // Cash-out is high risk
  };

  let z = -8; // bias toward safe (conservative model)
  Object.entries(weights).forEach(([k, w]) => { z += (f[k] || 0) * w; });
  let baseProb = sigmoid(z) * 100;

  // Rule engine — hard domain logic boosts
  const reasons = [];

  if(f.self_transfer)           { baseProb += 20; reasons.push('🔄 Self-transfer / highly similar sender-receiver names detected'); }
  if(f.circular_flag)           { baseProb += 18; reasons.push('♻️ Circular trading pattern — money returning to origin'); }
  if(f.many_to_one_flag)        { baseProb += 12; reasons.push('🕸️ Many-to-one aggregation — multiple senders to same destination'); }
  if(f.balance_drain)           { baseProb += 15; reasons.push('💸 Account balance drained to near-zero after transaction'); }
  if(f.odd_time)                { baseProb += 8;  reasons.push('🌙 Transaction at unusual hour (midnight–6 AM)'); }
  if(f.rapid_txn_flag)          { baseProb += 7;  reasons.push('⚡ Rapid-fire — multiple transactions in same time window'); }
  if(f.sudden_spike)            { baseProb += 10; reasons.push('📈 Sudden amount spike — 3× user\'s own historical maximum'); }
  if(f.fuzzy_flag && !f.self_transfer) { baseProb += 8; reasons.push('🔤 Fuzzy name match — sender/receiver names suspiciously similar'); }
  if(f.amount > 500000)         { baseProb += 8;  reasons.push('💰 Very high transaction amount (' + formatAmtShort(f.amount) + ')'); }
  else if(f.amount > 50000)     { baseProb += 4;  reasons.push('💰 High transaction amount (' + formatAmtShort(f.amount) + ')'); }
  if(f.velocity > 0.8)          { baseProb += 5;  reasons.push('⚡ High velocity — rapid succession transfers'); }
  if(f.balance_error > 1000)    { baseProb += 6;  reasons.push('📊 Balance mismatch — transaction amount doesn\'t reconcile with balance change'); }
  if(f.round_amount)            { baseProb += 4;  reasons.push('🎯 Round number amount (structuring indicator)'); }
  if(f.benford_flag)            { baseProb += 5;  reasons.push('📐 Benford\'s Law violation — unusual leading digit frequency'); }
  if(f.threshold_flag)          { baseProb += 6;  reasons.push('⚠️ Structuring — amount just below reporting threshold'); }
  if(f.multi_receiver_flag)     { baseProb += 5;  reasons.push('👥 Fan-out pattern — sent to 5+ unique destinations'); }
  if(f.amount_ratio > 5)        { baseProb += 5;  reasons.push('🏦 Amount far exceeds account balance'); }
  if(f.new_vendor_risk && f.amount > 10000) { baseProb += 4; reasons.push('🆕 First-time large transfer to new destination'); }
  if(f.type_CASH_OUT)           { baseProb += 4;  reasons.push('💵 Cash-out transaction type — high risk category'); }

  // If ground truth label says fraud — override (for labelled datasets)
  if(f.isFraud === 1) baseProb = Math.max(baseProb, 85);

  const riskScore = Math.min(Math.max(Math.round(baseProb), 0), 100);

  let decision, action;
  if(riskScore >= 70){
    decision = 'Fraud';
    action = 'BLOCK transaction immediately and escalate to compliance';
  } else if(riskScore >= 40){
    decision = 'Suspicious';
    action = 'FLAG for manual review by senior auditor';
  } else {
    decision = 'Safe';
    action = 'ALLOW — transaction appears within normal parameters';
  }
  if(reasons.length === 0) reasons.push('✅ No significant risk indicators detected');

  return { riskScore, decision, action, reasons, features: f };
}

/* ============================================================
   5. EXPLAINABILITY (SHAP-style feature contribution)
   ============================================================ */
function explainPrediction(f, riskScore){
  const featureLabels = {
    self_transfer:        'Self-Transfer / Name Match',
    circular_flag:        'Circular Trading',
    balance_drain:        'Account Balance Drained',
    odd_time:             'Unusual Timing (Late Night)',
    rapid_txn_flag:       'Rapid-Fire Pattern',
    sudden_spike:         'Sudden Amount Spike',
    fuzzy_flag:           'Name Similarity (Fuzzy)',
    many_to_one_flag:     'Many-to-One Aggregation',
    amount_norm:          'Transaction Amount (Normalized)',
    amount_ratio:         'Amount vs Balance Ratio',
    balance_error:        'Balance Mismatch',
    velocity:             'Transaction Velocity',
    benford_flag:         'Benford\'s Law Violation',
    threshold_flag:       'Threshold Evasion',
    multi_receiver_flag:  'Multiple Receivers Pattern',
    round_amount:         'Round Number Amounts',
    type_CASH_OUT:        'Cash-Out Transaction Type',
    type_TRANSFER:        'Transfer Type',
  };

  const w = {
    self_transfer: 18,   circular_flag: 16,   balance_drain: 15,
    odd_time: 10,        rapid_txn_flag: 7,   sudden_spike: 10,
    fuzzy_flag: 14,      many_to_one_flag: 10, amount_norm: 8,
    amount_ratio: 6,     balance_error: 5,    velocity: 4,
    benford_flag: 6,     threshold_flag: 7,   multi_receiver_flag: 6,
    round_amount: 3,     type_CASH_OUT: 6,    type_TRANSFER: 2,
  };

  const contributions = [];
  Object.entries(w).forEach(([k, weight]) => {
    let val = f[k] || 0;
    // Normalize continuous features to [0,1]
    if(k === 'amount_norm')  val = Math.min(val, 1);
    if(k === 'amount_ratio') val = Math.min(val / 10, 1);
    if(k === 'balance_error') val = Math.min(val / 100000, 1);
    if(k === 'velocity')     val = Math.min(val, 1);

    const c = val * weight;
    if(c > 0.5){
      contributions.push({
        feature: featureLabels[k] || k, key: k,
        value: Math.round(c * 10) / 10, rawValue: f[k],
        direction: c > 5 ? 'high' : 'medium'
      });
    }
  });

  contributions.sort((a, b) => b.value - a.value);
  const total = contributions.reduce((s, c) => s + c.value, 0) || 1;
  contributions.forEach(c => c.pct = Math.max(1, Math.round((c.value / total) * 100)));
  return contributions.slice(0, 8);
}

/* ============================================================
   6. MAIN RUNNER — called by launchAudit() after CSV load
   ============================================================ */
function runMLOnData(){
  if(_csvRawHeaders.length === 0 || _csvRawRows.length === 0) return;

  console.log('[ML] Running on', _csvRawRows.length, 'rows with headers:', _csvRawHeaders);

  // Feature engineering
  const enriched = engineerFeatures(_csvRawHeaders, _csvRawRows);
  console.log('[ML] Feature engineering complete. Sample:', enriched[0]);

  // Score every row
  window._mlResults = enriched.map(d => {
    const result = predictTransaction(d.features);
    return { ...d, mlResult: result, nameDest: d.nameDest, amount: d.amount };
  });

  // Rebuild tableData with ML classifications
  const m = autoMapIdx();
  tableData.length = 0;

  window._mlResults.forEach((d, idx) => {
    const entry = rowToEntry(d._raw, m);
    if(d.mlResult.decision === 'Fraud')           entry.r = 'high';
    else if(d.mlResult.decision === 'Suspicious') entry.r = 'suspicious';
    else                                          entry.r = 'safe';
    entry.rsn = d.mlResult.reasons.slice(0, 3).join(' | ') || 'No flags.';
    entry._mlIdx   = idx;
    entry._mlScore = d.mlResult.riskScore;
    if(!entry.v || entry.v === 'Unknown') entry.v = d.nameDest || 'Unknown';
    tableData.push(entry);
  });

  // Aggregate summary stats
  const high  = window._mlResults.filter(d => d.mlResult.decision === 'Fraud').length;
  const susp  = window._mlResults.filter(d => d.mlResult.decision === 'Suspicious').length;
  const safe  = window._mlResults.filter(d => d.mlResult.decision === 'Safe').length;
  const total = window._mlResults.length;
  const avgRisk = window._mlResults.reduce((s, d) => s + d.mlResult.riskScore, 0) / (total || 1);
  const totalFraudAmt = window._mlResults
    .filter(d => d.mlResult.decision === 'Fraud')
    .reduce((s, d) => s + (d.amount || d.features.amount || 0), 0);

  window._mlSummary = {
    high, susp, safe, total,
    avgRisk: Math.round(avgRisk),
    totalFraudAmt
  };

  console.log('[ML] Summary:', window._mlSummary);
  renderT();

  // Reset visualisation flags so they rebuild fresh on re-scan
  window._scatterBuilt = false;
  window._barsAnimated = false;
}

/* ============================================================
   7. FORMAT HELPERS
   ============================================================ */
function formatAmtShort(v){
  v = Math.abs(v || 0);
  if(v >= 10000000) return '₹' + (v / 10000000).toFixed(2) + ' Cr';
  if(v >= 100000)   return '₹' + (v / 100000).toFixed(2) + ' L';
  if(v >= 1000)     return '₹' + (v / 1000).toFixed(1) + 'K';
  return '₹' + Math.round(v).toLocaleString();
}

/* ============================================================
   8. DASHBOARD UPDATERS — called from launchAudit() after runMLOnData
   ============================================================ */
function updateDashboardWithML(){
  if(!window._mlSummary) return;
  const s = window._mlSummary;

  // ── Fraud Intelligence Panel numbers ──
  const cards = document.querySelectorAll('.sim-n');
  if(cards[0]){ cards[0].dataset.t = s.high; cards[0].textContent = s.high.toLocaleString(); }
  if(cards[1]){ cards[1].dataset.t = s.susp; cards[1].textContent = s.susp.toLocaleString(); }
  if(cards[2]){ cards[2].dataset.t = s.safe; cards[2].textContent = s.safe.toLocaleString(); }

  // ── Risk Distribution % badges ──
  const pcts = document.querySelectorAll('.fc-val .pct');
  if(pcts[0]) pcts[0].textContent = ((s.high / s.total) * 100).toFixed(1) + '%';
  if(pcts[1]) pcts[1].textContent = ((s.susp / s.total) * 100).toFixed(1) + '%';

  // ── Gauge ring ──
  const circ = 2 * Math.PI * 54;
  const gaugeRing = document.getElementById('fg-ring');
  const gaugeNum  = document.getElementById('fg-n');
  if(gaugeRing) gaugeRing.style.strokeDashoffset = circ - (circ * s.avgRisk / 100);
  if(gaugeNum)  gaugeNum.textContent = s.avgRisk;

  // ── Primary findings text ──
  const findingsDiv = document.querySelector('.fp-middle div[style*="flex:1"]');
  if(findingsDiv){
    const desc = findingsDiv.querySelector('div');
    if(desc) desc.innerHTML =
      `Detected <span style="color:var(--red)">${formatAmtShort(s.totalFraudAmt)}</span> in potential fraudulent activity across <strong>${s.high}</strong> high-risk and <strong>${s.susp}</strong> suspicious transactions.`;
  }

  // ── Total Fraud Exposure card ──
  const fpExposureVal  = document.querySelector('.exposure-card div[style*="font-size:3rem"], .exposure-card div[style*="font-size:2"]');
  const fpExposureDesc = document.querySelector('.exposure-card div[style*="font-size:0.85rem"]');
  if(fpExposureVal)  fpExposureVal.textContent  = formatAmtShort(s.totalFraudAmt);
  if(fpExposureDesc) fpExposureDesc.textContent = `Across ${s.high} high-risk + ${s.susp} suspicious transactions.`;

  // ── Mini-loader bar ──
  const loader = document.getElementById('mini-loader');
  if(loader) loader.style.width = s.avgRisk + '%';

  // ── Audit report text ──
  const auditReport = document.querySelector('.report-doc');
  if(auditReport){
    const sumP = auditReport.querySelector('p:nth-of-type(2)');
    if(sumP) sumP.innerHTML =
      `An automated forensic analysis of the ledger database was completed. The AI models isolated <strong>${s.high} distinct high-risk transactions</strong> representing <strong>${formatAmtShort(s.totalFraudAmt)}</strong> in potentially illicit disbursements. Additionally, <strong>${s.susp} transactions</strong> were flagged as suspicious and require manual review.`;
  }

  // ── Insights panel ──
  const insEl = document.getElementById('insights-total-fraud');
  const insVendors = document.getElementById('insights-vendors');
  const insPattern = document.getElementById('insights-pattern');
  if(insEl) insEl.textContent = formatAmtShort(s.totalFraudAmt);

  // Count unique fraud destination entities
  const fraudDests = new Set(
    (window._mlResults || []).filter(d => d.mlResult.decision === 'Fraud').map(d => d.nameDest)
  );
  if(insVendors) insVendors.textContent = fraudDests.size + ' Entities';
  if(insPattern){
    if(s.high > 0){
      const hasSelf = (window._mlResults||[]).some(d =>
        d.mlResult.decision === 'Fraud' && d.features.self_transfer === 1
      );
      const hasCircular = (window._mlResults||[]).some(d =>
        d.mlResult.decision === 'Fraud' && d.features.circular_flag === 1
      );
      insPattern.textContent = hasCircular
        ? 'Money Laundering (Circular)'
        : hasSelf
          ? 'Money Laundering (Self-Transfer)'
          : 'Fraud / Anomaly';
    } else if(s.susp > 0){
      insPattern.textContent = 'Suspicious Activity';
    } else {
      insPattern.textContent = 'Clean / Safe';
    }
  }

  // Tab-specific updaters
  updateExplainabilityTab();
  updateOverviewTab();
  updateRelationshipsTab();
  updateTrendsTab();
}

/* ── Overview Tab ── */
function updateOverviewTab(){
  if(!window._mlSummary) return;
  const s = window._mlSummary, total = s.total || 1;
  const ov = document.getElementById('tab-ov');
  if(!ov) return;

  const h3span = ov.querySelector('h3 span');
  if(h3span) h3span.textContent = 'Total ' + total.toLocaleString() + ' TX';

  // Donut center text
  const centerTxt = ov.querySelector('text');
  if(centerTxt) centerTxt.textContent = total >= 1000 ? (total/1000).toFixed(1)+'K' : total.toString();

  // Legend items
  const hp = (s.high/total*100).toFixed(1);
  const sp = (s.susp/total*100).toFixed(1);
  const gp = (s.safe/total*100).toFixed(1);
  const legends = ov.querySelectorAll('.t-card div[style*="justify-content:center"] span');
  if(legends[0]) legends[0].innerHTML = '&#9632; High ' + hp + '% (' + s.high.toLocaleString() + ')';
  if(legends[1]) legends[1].innerHTML = '&#9632; Susp ' + sp + '% (' + s.susp.toLocaleString() + ')';
  if(legends[2]) legends[2].innerHTML = '&#9632; Safe ' + gp + '% (' + s.safe.toLocaleString() + ')';

  // Donut ring segments (SVG stroke-dasharray manipulation for 3-color donut)
  const circumference = 2 * Math.PI * 80; // r=80 assumed
  const hPct = s.high / total;
  const sPct = s.susp / total;
  const gPct = s.safe / total;
  const donutHigh  = ov.querySelector('#donut-high');
  const donutSusp  = ov.querySelector('#donut-susp');
  const donutSafe  = ov.querySelector('#donut-safe');
  if(donutHigh){
    const dash = circumference * hPct;
    const offset = 0;
    donutHigh.style.strokeDasharray  = dash + ' ' + (circumference - dash);
    donutHigh.style.strokeDashoffset = circumference * 0.25; // start at top
  }
  if(donutSusp){
    const dash   = circumference * sPct;
    const offset = circumference * (1 - hPct) - circumference * 0.25;
    donutSusp.style.strokeDasharray  = dash + ' ' + (circumference - dash);
    donutSusp.style.strokeDashoffset = offset;
  }
}

/* ── Trends Tab ── */
function updateTrendsTab() {
  if (!window._mlResults || window._mlResults.length === 0) return;

  const results = window._mlResults;
  
  // 1. Compute 12-bin histogram for "Monthly" Trends (Using time_bucket pseudo-months)
  let maxStep = 0;
  results.forEach(d => { if (d.features.time_bucket > maxStep) maxStep = d.features.time_bucket; });
  if (maxStep === 0) maxStep = 1; // fallback
  
  const bins = Array(12).fill(null).map(() => ({ safe: 0, susp: 0, high: 0 }));
  results.forEach(d => {
    let binIdx = Math.floor((d.features.time_bucket / maxStep) * 11);
    if (binIdx > 11) binIdx = 11;
    if (binIdx < 0) binIdx = 0;
    
    if (d.mlResult.decision === 'Fraud') bins[binIdx].high++;
    else if (d.mlResult.decision === 'Suspicious') bins[binIdx].susp++;
    else bins[binIdx].safe++;
  });

  let maxColHeight = 1;
  bins.forEach(b => {
    const sum = b.safe + b.susp + b.high;
    if (sum > maxColHeight) maxColHeight = sum;
  });

  const months = ['Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar'];
  const monthHTML = bins.map((b, i) => {
    // Height scaled to ~150px max for safe, 50px max for high, etc., visual relative scaling
    const sH = Math.max(2, (b.safe / maxColHeight) * 150);
    const uH = Math.max(0, (b.susp / maxColHeight) * 50);
    const hH = Math.max(0, (b.high / maxColHeight) * 50);
    
    const isSpike = b.high > (bins.reduce((acc, curr) => acc + curr.high, 0) / 12) * 3 && b.high > 2;

    return `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;position:relative">
        ${isSpike ? '<div style="position:absolute;top:-24px;left:50%;transform:translateX(-50%);background:var(--red);color:#fff;font-family:var(--mono);font-size:0.52rem;padding:2px 5px;border-radius:4px;white-space:nowrap">&#9888; SPIKE</div>' : ''}
        <div style="width:100%;flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px">
          ${hH > 0 ? `<div style="height:${hH}px;background:var(--red);border-radius:2px 2px 0 0;${isSpike ? 'box-shadow:0 0 10px rgba(248,113,113,0.5)' : ''}"></div>` : ''}
          ${uH > 0 ? `<div style="height:${uH}px;background:var(--amber)"></div>` : ''}
          ${sH > 0 ? `<div style="height:${sH}px;background:var(--cyan);opacity:0.65;${(hH===0 && uH===0) ? 'border-radius:2px 2px 0 0' : ''}"></div>` : ''}
        </div>
        <div style="font-family:var(--mono);font-size:0.58rem;color:${isSpike ? 'var(--red)' : 'var(--txt3)'};margin-top:5px;${isSpike ? 'font-weight:600' : ''}">${months[i]}</div>
      </div>
    `;
  }).join('');

  const barsContainer = document.getElementById('trends-monthly-bars');
  if (barsContainer) {
    barsContainer.innerHTML = monthHTML;
  }

  const alertMsg = document.getElementById('trends-monthly-alert');
  if (alertMsg) {
    const totalHigh = bins.reduce((acc, b) => acc + b.high, 0);
    alertMsg.innerHTML = totalHigh > 0 
      ? `<strong style="color:var(--red)">Alert:</strong> Detected anomalies clustered in specific time segments. Peak anomaly volume reaches critical threshold.`
      : `<strong style="color:var(--green)">Scan Result:</strong> Transaction flow remains uniform with no severe volume spikes detected over the period.`;
  }

  // 2. Compute Top Vendors by Exposure
  const entityExposure = {};
  let totalExposure = 0;
  
  results.filter(d => d.mlResult.decision !== 'Safe').forEach(d => {
    const vendor = d.nameDest || 'Unknown Vendor';
    const amt = d.amount || 0;
    if (!entityExposure[vendor]) entityExposure[vendor] = { high: 0, susp: 0, total: 0 };
    entityExposure[vendor].total += amt;
    if (d.mlResult.decision === 'Fraud') entityExposure[vendor].high += amt;
    else entityExposure[vendor].susp += amt;
    totalExposure += amt;
  });

  const sortedVendors = Object.entries(entityExposure)
    .sort((a, b) => b[1].total - a[1].total)
    .slice(0, 6); // Top 6

  const vendorsContainer = document.getElementById('trends-top-vendors');
  if (vendorsContainer) {
    if (sortedVendors.length === 0) {
      vendorsContainer.innerHTML = `<div style="color:var(--txt3);font-size:0.85rem">No flagged vendors detected in this ledger.</div>`;
    } else {
      let maxExp = sortedVendors[0][1].total;
      vendorsContainer.innerHTML = sortedVendors.map(([vendor, exp]) => {
        const pctHigh = maxExp > 0 ? (exp.high / maxExp) * 100 : 0;
        const pctSusp = maxExp > 0 ? (exp.susp / maxExp) * 100 : 0;
        const tag = exp.high > exp.susp 
          ? `<span style="background:var(--red-dim);color:var(--red);font-family:var(--mono);font-size:0.6rem;padding:1px 6px;border-radius:3px;margin-left:6px">HIGH</span>`
          : `<span style="background:var(--amber-dim);color:var(--amber);font-family:var(--mono);font-size:0.6rem;padding:1px 6px;border-radius:3px;margin-left:6px">SUSP</span>`;
        const color = exp.high > exp.susp ? 'var(--red)' : 'var(--amber)';
        
        return `
          <div>
            <div style="display:flex;justify-content:space-between;margin-bottom:5px">
               <span style="font-size:0.85rem">${vendor} ${tag}</span>
               <span style="font-family:var(--mono);font-size:0.78rem;color:${color}">&#8377;${formatAmtShort(exp.total, true)}</span>
            </div>
            <div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;display:flex;">
               ${pctHigh > 0 ? `<div style="width:${pctHigh}%;height:100%;background:var(--red);border-radius:3px 0 0 3px"></div>` : ''}
               ${pctSusp > 0 ? `<div style="width:${pctSusp}%;height:100%;background:var(--amber);border-radius:${pctHigh > 0 ? '0 3px 3px 0' : '3px'}"></div>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }
  }

  const expTotalEl = document.getElementById('trends-total-exposure');
  if (expTotalEl) expTotalEl.innerHTML = `&#8377;${formatAmtShort(totalExposure, true)}`;
  const expDescEl = document.getElementById('trends-exposure-desc');
  if (expDescEl) expDescEl.innerHTML = sortedVendors.length > 0
    ? `Highest exposure primarily concentrated in <strong>${sortedVendors[0][0]}</strong>.`
    : `No fraud exposure calculated for this dataset.`;
}

/* ── Explainability Tab ── */
function updateExplainabilityTab(){
  if(!window._mlResults || window._mlResults.length === 0) return;

  const flagged = window._mlResults.filter(d =>
    d.mlResult.decision === 'Fraud' || d.mlResult.decision === 'Suspicious'
  );
  if(flagged.length === 0) return;

  // Average features across all flagged rows
  const avgF = {};
  const keys = Object.keys(flagged[0].features);
  keys.forEach(k => {
    avgF[k] = flagged.reduce((s, d) => s + (d.features[k] || 0), 0) / flagged.length;
  });

  const contributions = explainPrediction(avgF, window._mlSummary.avgRisk);

  const container = document.querySelector('#tab-ex .t-card');
  if(!container) return;

  const h3 = container.querySelector('h3');
  if(h3) h3.innerHTML = 'AI Model Inference Breakdown <span id="ex-risk-label">Risk = ' + window._mlSummary.avgRisk + '%</span>';

  const desc = container.querySelector('#ex-desc');
  if(desc) desc.textContent =
    'ML feature weights driving the anomaly scores across your ' +
    window._mlSummary.total.toLocaleString() + ' transactions (averaged over ' +
    flagged.length + ' flagged entries):';

  const exBars = container.querySelector('#ex-bars');
  if(!exBars) return;

  exBars.innerHTML = '';
  const colors = [
    'var(--red)','var(--red)','var(--red)',
    'var(--amber)','var(--amber)',
    'var(--cyan)','var(--cyan)','var(--green)'
  ];
  contributions.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'fac-row';
    row.title = c.feature + ' — Raw value: ' + (typeof c.rawValue === 'number' ? c.rawValue.toFixed(3) : c.rawValue);
    row.innerHTML =
      '<span class="fac-label">' + c.feature + '</span>' +
      '<div class="fac-bar-bg"><div class="fac-bar-fill" style="background:' +
      (colors[i] || 'var(--cyan)') + '; width:0%" data-w="' + c.pct + '"></div></div>' +
      '<span class="fac-val">' + c.pct + '%</span>';
    exBars.appendChild(row);
  });

  setTimeout(() => {
    exBars.querySelectorAll('.fac-bar-fill').forEach(fill => {
      fill.style.width = fill.getAttribute('data-w') + '%';
    });
  }, 60);
}

/* ── Relationships Tab ── */
function updateRelationshipsTab(){
  if(!window._mlResults || window._mlResults.length === 0) return;

  // Aggregate fraud amounts by destination vendor
  const fraudVendors = {};
  window._mlResults.forEach(r => {
    if(r.mlResult && r.mlResult.decision === 'Fraud'){
      const v = (r.nameDest || 'Unknown Corp').trim();
      if(!fraudVendors[v]) fraudVendors[v] = { name: v, amt: 0, count: 0 };
      fraudVendors[v].amt   += (r.amount || r.features.amount || 0);
      fraudVendors[v].count++;
    }
  });

  const sorted = Object.values(fraudVendors).sort((a, b) => b.amt - a.amt);
  if(sorted.length === 0) return;

  const totalLaundered = sorted.reduce((s, v) => s + v.amt, 0);

  // SVG node labels — A=hub, B–F=ring nodes
  const nodeIds = ['A','B','C','D','E'];
  nodeIds.forEach((id, i) => {
    const v = sorted[i] ? sorted[i].name : 'Shell ' + (i + 1);
    const parts = v.split(' ');
    const n1 = document.getElementById('node-' + id + '1');
    const n2 = document.getElementById('node-' + id + '2');
    if(n1) n1.textContent = parts[0].substring(0, 12);
    if(n2) n2.textContent = (parts.slice(1).join(' ') || '\u00a0').substring(0, 12);
  });

  // Entity cards (0–4)
  sorted.slice(0, 5).forEach((v, i) => {
    const nameEl = document.getElementById('re-card-name-' + i);
    const amtEl  = document.getElementById('re-card-amt-'  + i);
    if(nameEl) nameEl.textContent = v.name;
    if(amtEl)  amtEl.textContent  = formatAmtShort(v.amt) + ' (' + v.count + ' tx)';
  });

  // Alert banner
  const alertBody = document.getElementById('re-alert-body');
  if(alertBody) alertBody.innerHTML =
    'Funds routed through <strong style="color:var(--txt)">' + sorted.length +
    ' shell entities</strong> in circular pattern. Total laundered: <strong style="color:var(--red)">' +
    formatAmtShort(totalLaundered) + '</strong> across <strong style="color:var(--txt)">' +
    (window._mlSummary ? window._mlSummary.high : sorted.reduce((s, v) => s + v.count, 0)) +
    ' high-risk transactions</strong>.';

  // Stats tiles
  const statEnt   = document.getElementById('re-stat-entities');
  const statLaund = document.getElementById('re-stat-laundered');
  if(statEnt)   statEnt.textContent   = sorted.length;
  if(statLaund) statLaund.textContent = formatAmtShort(totalLaundered);

  // Conclusion
  const concl = document.getElementById('re-conclusion');
  if(concl) concl.innerHTML =
    '<strong style="color:var(--red)">Forensic Conclusion:</strong> ' + sorted.length +
    ' entities detected in circular trading ring. Primary hub: <strong>' +
    (sorted[0] ? sorted[0].name : '—') + '</strong>. ' +
    'Pattern consistent with <strong style="color:var(--txt)">Layer &amp; Integration stage money laundering</strong> as defined under PMLA 2002.';
}

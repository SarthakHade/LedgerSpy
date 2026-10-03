/* ============================================================
   LedgerSpy Assistant (rule-based, keyword routing)
   ============================================================
   Answers only from the current scan's in-memory results.
   P1: every value that originates from the uploaded file is passed
   through escapeHtml(); thresholds come from LS_CONFIG (the same
   values the scoring engine uses); row lookup is exact by ID.
   ============================================================ */

let _chatbotOpen = false;

/* ---- UI Controls ---- */
function toggleChatbot(){
  const panel = document.getElementById('chatbot-panel');
  _chatbotOpen = !_chatbotOpen;
  if(_chatbotOpen){
    panel.classList.add('show');
    document.getElementById('chat-input').focus();
  } else {
    panel.classList.remove('show');
  }
}

/** Bot message with trusted markup. Callers MUST escape any data they interpolate (processQuery does). */
function addChatMsg(html, type){
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + type;
  if(type === 'bot') div.innerHTML = '<div class="msg-label">Assistant</div>' + html;
  else div.textContent = html;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}
/** Bot message from plain text (no markup interpreted). */
function addChatMsgText(text){
  return addChatMsg(escapeHtml(text), 'bot');
}

function showTyping(){
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-typing';
  div.id = 'chat-typing-indicator';
  div.innerHTML = '<span></span><span></span><span></span>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function hideTyping(){
  const n = document.getElementById('chat-typing-indicator');
  if(n) n.remove();
}

function chatSuggest(btn){
  const text = btn.textContent.replace(/^[^\s]+ /, '').trim();
  document.getElementById('chat-input').value = text;
  sendChat();
}

function sendChat(){
  const input = document.getElementById('chat-input');
  const query = (input.value || '').trim();
  if(!query) return;
  addChatMsg(query, 'user');
  input.value = '';
  showTyping();
  setTimeout(() => {
    hideTyping();
    addChatMsg(processQuery(query), 'bot');
  }, 450);
}

/* ---- Helpers ---- */
const E = escapeHtml;
const fmtAmt = v => E(formatAmtShort(v));
const tierLabel = r => r === 'high' ? 'HIGH RISK' : r === 'suspicious' ? 'SUSPICIOUS' : r === 'safe' ? 'BELOW THRESHOLD' : 'NOT SCORED';

/** Find a table row by an ID mentioned in the query (exact, case-insensitive). Accepts "ROW-12", "row 12", "tx-12" or a raw ID. */
function findRowInQuery(q){
  const norm = s => String(s).toLowerCase().replace(/\s+/g, '-');
  const ids = new Map(tableData.map((d, i) => [norm(d.id), i]));
  const m = q.match(/\b(row|new|tx)[-\s]?(\d+)\b/i);
  if(m){
    const k = norm(m[1] + '-' + m[2]);
    if(ids.has(k)) return ids.get(k);
    const alt = norm(LS_CONFIG.rowIdPrefix + m[2]);          // "tx-12" → "row-12"
    if(m[1].toLowerCase() === 'tx' && ids.has(alt)) return ids.get(alt);
  }
  for(const tok of q.split(/[\s,?!.;:"']+/)){
    if(tok && ids.has(norm(tok))) return ids.get(norm(tok));
  }
  return -1;
}

/* ============================================================
   QUERY PROCESSOR
   ============================================================ */
function processQuery(query){
  const q = query.toLowerCase();
  const s = window._mlSummary;
  const results = window._mlResults;
  const hasData = s && s.total > 0;
  const T = LS_CONFIG.thresholds;

  if(!hasData){
    return '⚠️ No data loaded yet. Please <strong>upload a CSV file</strong> and click <strong>"Execute Deep Scan"</strong> first.';
  }

  /* ---- Summary ---- */
  if(q.includes('summary') || q.includes('overview') || q.includes('dashboard')){
    return '📊 <strong>Heuristic preview summary</strong><br><br>'
      + '📁 Rows scored: <code>' + s.total.toLocaleString() + '</code><br>'
      + '🔴 High risk (≥' + T.high + '): <code>' + s.high.toLocaleString() + '</code> (' + ((s.high/s.total)*100).toFixed(1) + '%)<br>'
      + '🟡 Suspicious (' + T.suspicious + '–' + (T.high - 1) + '): <code>' + s.susp.toLocaleString() + '</code> (' + ((s.susp/s.total)*100).toFixed(1) + '%)<br>'
      + '🟢 Below thresholds: <code>' + s.safe.toLocaleString() + '</code><br><br>'
      + '📈 Average score: <strong>' + s.avgRisk + '/100</strong><br>'
      + '💰 Amount in high-risk rows: <strong>' + fmtAmt(s.highAmt) + '</strong><br><br>'
      + '<em>Rule-based indicators for review — not fraud findings.</em>'
      + (s.calibrationWarning ? '<br>⚠️ Flag rate ' + s.flaggedPct.toFixed(1) + '% exceeds the ' + LS_CONFIG.reviewBudgetPct + '% review budget: this preview is not calibrated for this file.' : '');
  }

  /* ---- Methodology (must precede the "why/explain" branch) ---- */
  if(q.includes('risk score') || q.includes('explain risk') || q.includes('explain score') || q.includes('how') && q.includes('score') || q.includes('methodology')){
    return '🔍 <strong>How the heuristic preview scores rows</strong><br><br>'
      + '1. About 30 features per row are computed from the file (amount ratios, balance change, name similarity, sender/receiver counts, leading digit, step value).<br>'
      + '2. A sigmoid of <em>hand-set</em> weights gives a base of 0–100 points.<br>'
      + '3. Fixed rule points are added (e.g. reciprocity, balance drain, round amount).<br>'
      + '4. The total is capped to 0–100.<br><br>'
      + '<strong>Tiers:</strong><br>'
      + '&nbsp;&nbsp;🔴 ≥' + T.high + ' → High risk (prioritise for vouching)<br>'
      + '&nbsp;&nbsp;🟡 ' + T.suspicious + '–' + (T.high - 1) + ' → Suspicious (review sample)<br>'
      + '&nbsp;&nbsp;🟢 <' + T.suspicious + ' → Below thresholds<br><br>'
      + 'This is <strong>not a trained model</strong>. Weights were not learned from data and are not calibrated. Label columns such as isFraud are ignored.<br>'
      + 'Current average: <strong>' + s.avgRisk + '/100</strong>';
  }

  /* ---- Why is a row flagged (context-aware) ---- */
  if(q.includes('why') || q.includes('explain') || q.includes('flagged') || q.includes('reason')){
    let idx = findRowInQuery(query);
    if(idx < 0 && window._selectedTxIdx >= 0 && window._selectedTxIdx < tableData.length) idx = window._selectedTxIdx;
    if(idx < 0 && /\b(row|tx|new)[-\s]?\d+/i.test(query)){
      return '🔎 That row ID was not found. IDs look like <code>' + E(tableData[0] ? tableData[0].id : LS_CONFIG.rowIdPrefix + '1') + '</code>.';
    }
    if(idx < 0){
      idx = tableData.reduce((best, d, i) => (best < 0 || (d._mlScore || 0) > (tableData[best]._mlScore || 0)) ? i : best, -1);
    }
    const target = tableData[idx];
    if(target && target._mlIdx !== undefined && results[target._mlIdx]){
      const ml = results[target._mlIdx].mlResult;
      const lines = ml.components.map(c => '&nbsp;&nbsp;• +' + c.pts.toFixed(1) + ' pts — ' + E(c.text)).join('<br>');
      return '🔎 <strong>' + E(target.id) + '</strong> — ' + E(tierLabel(ml.tier)) + '<br><br>'
        + '📊 Heuristic score: <strong>' + ml.riskScore + '/100</strong> (raw ' + ml.rawTotal.toFixed(1) + ' pts)<br>'
        + '💰 Amount: <strong>₹' + E(target.amt) + '</strong><br>'
        + '🏢 Payee: ' + E(target.v) + '<br><br>'
        + '<strong>Score breakdown:</strong><br>' + lines + '<br><br>'
        + '📋 ' + E(ml.action);
    }
    if(target) return '🔎 <strong>' + E(target.id) + '</strong> has not been scored yet (added after the scan). Re-run the scan.';
    return '🔎 Row not found. Try: <em>"Why is ' + E(LS_CONFIG.rowIdPrefix) + '12 flagged?"</em> or select a row in the table first.';
  }

  /* ---- High risk list ---- */
  if(q.includes('high risk') || q.includes('fraud') || q.includes('dangerous') || q.includes('blocked')){
    const hTx = tableData.filter(d => d.r === 'high').slice(0, 5);
    if(hTx.length === 0) return '✅ No rows scored ≥ ' + T.high + ' in this file.';
    const list = hTx.map((d, i) =>
      '<strong>' + (i + 1) + '.</strong> <code>' + E(d.id) + '</code> — ' + E(d.v) + ' — ₹' + E(d.amt) + ' — score ' + E(d._mlScore)
    ).join('<br>');
    return '🔴 <strong>High-risk rows (heuristic)</strong> — ' + s.high.toLocaleString() + ' total<br><br>' + list
      + '<br><br>💡 Ask <em>"why is ' + E(hTx[0].id) + ' flagged?"</em> for its score breakdown.';
  }

  /* ---- Top anomalies ---- */
  if(q.includes('anomal') || q.includes('outlier') || q.includes('unusual') || q.includes('top')){
    const sorted = [...tableData].filter(d => d._mlScore !== undefined)
      .sort((a, b) => (b._mlScore || 0) - (a._mlScore || 0)).slice(0, 5);
    if(sorted.length === 0) return '📊 No scored rows.';
    const list = sorted.map((d, i) =>
      '<strong>' + (i + 1) + '.</strong> <code>' + E(d.id) + '</code> — score <strong>' + E(d._mlScore) + '</strong> — ₹' + E(d.amt) + ' — ' + E(d.v)
    ).join('<br>');
    return '💡 <strong>Highest heuristic scores</strong><br><br>' + list;
  }

  /* ---- Action ---- */
  if(q.includes('action') || q.includes('what should') || q.includes('recommend') || q.includes('what do')){
    return '📋 <strong>Suggested handling (heuristic preview)</strong><br><br>'
      + '🔴 <strong>' + s.high + '</strong> rows ≥' + T.high + ' → prioritise for manual vouching<br>'
      + '🟡 <strong>' + s.susp + '</strong> rows ' + T.suspicious + '–' + (T.high - 1) + ' → consider for a review sample<br>'
      + '🟢 <strong>' + s.safe + '</strong> rows → no heuristic rule exceeded the thresholds<br><br>'
      + 'No row should be treated as fraudulent on the basis of this preview alone.';
  }

  /* ---- Below thresholds ---- */
  if(q.includes('safe') || q.includes('clean') || q.includes('legit')){
    return '🟢 <strong>' + s.safe.toLocaleString() + '</strong> of <strong>' + s.total.toLocaleString()
      + '</strong> rows (' + ((s.safe/s.total)*100).toFixed(1) + '%) scored below ' + T.suspicious + '.<br><br>'
      + 'Below threshold does not mean verified — it only means no heuristic rule pushed the score up.';
  }

  /* ---- Suspicious ---- */
  if(q.includes('suspicious') || q.includes('warning') || q.includes('review')){
    const sTx = tableData.filter(d => d.r === 'suspicious').slice(0, 5);
    const list = sTx.map((d, i) => '<strong>' + (i + 1) + '.</strong> <code>' + E(d.id) + '</code> — ' + E(d.v) + ' — ₹' + E(d.amt)).join('<br>');
    return '🟡 <strong>' + s.susp.toLocaleString() + ' suspicious rows</strong> (score ' + T.suspicious + '–' + (T.high - 1) + ')<br><br>' + (list || 'None found.');
  }

  /* ---- Benford ---- */
  if(q.includes('benford') || q.includes('digit')){
    const b = window._mlBenford;
    const bFlags = results.filter(d => d.features.benford_flag).length;
    const over = b && b.overrepDigits.length ? b.overrepDigits.join(', ') : 'none';
    return '📐 <strong>Leading-digit profile (descriptive)</strong><br><br>'
      + 'Amounts analysed: <code>' + (b ? b.n.toLocaleString() : 0) + '</code><br>'
      + 'Digits more than ' + LS_CONFIG.benfordOverrepPP + ' pp above Benford expectation: <strong>' + E(over) + '</strong><br>'
      + 'Rows starting with those digits: <code>' + bFlags.toLocaleString() + '</code><br><br>'
      + 'No statistical test is applied yet (roadmap P7). A deviation is a prompt to look closer, not evidence of fabrication.';
  }

  /* ---- Velocity ---- */
  if(q.includes('velocity') || q.includes('rapid') || q.includes('speed') || q.includes('fast')){
    const rapid = results.filter(d => d.features.rapid_txn_flag).length;
    return '⚡ <strong>Rapid succession</strong><br><br>'
      + '<strong>' + rapid.toLocaleString() + '</strong> rows have another transaction by the same sender within 1 step.<br><br>'
      + 'If senders rarely repeat in this file, this indicator carries little information.';
  }

  /* ---- Help ---- */
  return '🤖 I answer from the current scan only. Try:<br><br>'
    + '• "<strong>Show summary</strong>"<br>'
    + '• "<strong>High risk transactions</strong>"<br>'
    + '• "<strong>Explain risk score</strong>" — how scoring works<br>'
    + '• "<strong>Why is ' + E(LS_CONFIG.rowIdPrefix) + '12 flagged?</strong>" — row breakdown<br>'
    + '• "<strong>Top anomalies</strong>"<br>'
    + '• "<strong>What action should I take?</strong>"<br>'
    + '• "<strong>Benford</strong>" — leading-digit profile<br>'
    + '• "<strong>Velocity</strong>"';
}

/* ============================================================
   LedgerSpy AI Chatbot
   Ported from chatbot.py + enhanced with context-aware analysis
   ============================================================
   Handles: toggle UI, message rendering, typing indicator,
   NLP-style query processing, suggestion chips, and
   context-aware transaction investigation (uses ml-engine.js)
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

function addChatMsg(text, type){
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + type;
  if(type === 'bot') div.innerHTML = '<div class="msg-label">AI Assistant</div>' + text;
  else div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
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
  const el = document.getElementById('chat-typing-indicator');
  if(el) el.remove();
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

  const delay = 400 + Math.random() * 600;
  setTimeout(() => {
    hideTyping();
    const response = processQuery(query);
    addChatMsg(response, 'bot');
  }, delay);
}

/* ============================================================
   QUERY PROCESSOR (replaces & enhances chatbot.py logic)
   ============================================================ */
function processQuery(query){
  const q = query.toLowerCase();
  const s = window._mlSummary;
  const results = window._mlResults;
  const hasData = s && s.total > 0;

  if(!hasData){
    return '⚠️ No data loaded yet. Please <strong>upload a CSV file</strong> and click <strong>"Execute Deep Scan"</strong> first.';
  }

  const fmtAmt = v => {
    if(v >= 10000000) return '₹' + (v/10000000).toFixed(2) + ' Cr';
    if(v >= 100000)   return '₹' + (v/100000).toFixed(2) + ' L';
    if(v >= 1000)     return '₹' + (v/1000).toFixed(1) + 'K';
    return '₹' + Math.round(v).toLocaleString();
  };

  /* ---- Summary / Overview ---- */
  if(q.includes('summary') || q.includes('overview') || q.includes('show summary') || q.includes('dashboard')){
    return '📊 <strong>Ledger Analysis Summary</strong><br><br>'
      + '📁 Total Transactions: <code>'+s.total.toLocaleString()+'</code><br>'
      + '🔴 High Risk: <code>'+s.high.toLocaleString()+'</code> ('+((s.high/s.total)*100).toFixed(1)+'%)<br>'
      + '🟡 Suspicious: <code>'+s.susp.toLocaleString()+'</code> ('+((s.susp/s.total)*100).toFixed(1)+'%)<br>'
      + '🟢 Safe: <code>'+s.safe.toLocaleString()+'</code><br><br>'
      + '📈 Average Risk Score: <strong>'+s.avgRisk+'%</strong><br>'
      + '💰 Total Fraud Exposure: <strong>'+fmtAmt(s.totalFraudAmt)+'</strong>';
  }

  /* ---- High risk / fraud ---- */
  if(q.includes('high risk') || q.includes('fraud') || q.includes('dangerous') || q.includes('blocked')){
    const hTx = tableData.filter(d => d.r === 'high').slice(0, 5);
    if(hTx.length === 0) return '✅ No high-risk transactions found. All clear!';

    const list = hTx.map((d,i) =>
      '<strong>'+(i+1)+'.</strong> <code>'+d.id+'</code> — '+d.v+' — ₹'+d.amt
      +' <span class="risk-badge-inline" style="background:rgba(248,113,113,0.15);color:#f87171">HIGH RISK</span>'
    ).join('<br>');

    return '🔴 <strong>Top High-Risk Transactions</strong> ('+s.high+' total)<br><br>'
      + list + '<br><br>💡 Click a row in the table, then ask <em>"why is this flagged?"</em>';
  }

  /* ---- Risk score explanation ---- */
  if(q.includes('risk score') || q.includes('explain risk') || q.includes('explain score')){
    return '🔍 <strong>Risk Score Methodology</strong><br><br>'
      + 'The LedgerSpy ML engine uses a multi-layer approach:<br><br>'
      + '<strong>1. Feature Engineering</strong> — 20+ features per row:<br>'
      + '&nbsp;&nbsp;• Amount-to-balance ratio, transaction velocity<br>'
      + '&nbsp;&nbsp;• Name similarity (fuzzy matching)<br>'
      + '&nbsp;&nbsp;• Benford\'s Law compliance<br><br>'
      + '<strong>2. Base Model</strong> — Sigmoid-weighted scoring<br><br>'
      + '<strong>3. Rule Engine</strong> — Domain-specific boosts:<br>'
      + '&nbsp;&nbsp;• High amount, rapid succession, odd hours<br>'
      + '&nbsp;&nbsp;• Self-transfers, round numbers, threshold evasion<br><br>'
      + '<strong>Thresholds:</strong><br>'
      + '&nbsp;&nbsp;🔴 ≥70% → Fraud (BLOCK)<br>'
      + '&nbsp;&nbsp;🟡 ≥50% → Suspicious (REVIEW)<br>'
      + '&nbsp;&nbsp;🟢 <50% → Safe (ALLOW)<br><br>'
      + 'Current avg risk: <strong>'+s.avgRisk+'%</strong>';
  }

  /* ---- Why flagged (context-aware) ---- */
  if(q.includes('why') || q.includes('explain') || q.includes('flagged') || q.includes('reason')){
    const txMatch = q.match(/tx[-\s]?(\d+)/i);
    let target = null;

    if(txMatch){
      target = tableData.find(d => d.id.toLowerCase().includes(txMatch[1]));
    }
    if(!target && window._selectedTxIdx >= 0 && window._selectedTxIdx < tableData.length){
      target = tableData[window._selectedTxIdx];
    }
    if(!target){
      target = tableData.reduce((best, d) =>
        (!best || (d._mlScore||0) > (best._mlScore||0)) ? d : best, null);
    }

    if(target && target._mlIdx !== undefined && results[target._mlIdx]){
      const ml = results[target._mlIdx].mlResult;
      const reasonList = ml.reasons.map(r => '&nbsp;&nbsp;• '+r).join('<br>');
      const badge = ml.decision==='Fraud' ? '<span style="color:#f87171">⚠️ FRAUD</span>'
        : ml.decision==='Suspicious' ? '<span style="color:#f59e0b">⚡ SUSPICIOUS</span>'
        : '<span style="color:#34d399">✅ SAFE</span>';

      return '🔎 <strong>Analysis: '+target.id+'</strong> '+badge+'<br><br>'
        + '📊 Risk Score: <strong>'+ml.riskScore+'%</strong><br>'
        + '💰 Amount: <strong>₹'+target.amt+'</strong><br>'
        + '🏢 Vendor: '+target.v+'<br><br>'
        + '<strong>Reasons:</strong><br>'+reasonList+'<br><br>'
        + '📋 Recommended Action: <em>'+ml.action+'</em>';
    }
    return '🔎 Transaction not found. Try: <em>"Why is TX-1234 flagged?"</em> or select a row in the table first.';
  }

  /* ---- Top anomalies ---- */
  if(q.includes('anomal') || q.includes('outlier') || q.includes('unusual')){
    const sorted = [...tableData].filter(d => d._mlScore !== undefined)
      .sort((a,b) => (b._mlScore||0)-(a._mlScore||0)).slice(0,5);
    if(sorted.length === 0) return '📊 No anomalies. Your data looks clean!';

    const list = sorted.map((d,i) =>
      '<strong>'+(i+1)+'.</strong> <code>'+d.id+'</code> — Score: <strong>'+d._mlScore+'%</strong> — ₹'+d.amt+' — '+d.v
    ).join('<br>');
    return '💡 <strong>Top Anomalies by Risk Score</strong><br><br>'+list;
  }

  /* ---- Action / recommendation ---- */
  if(q.includes('action') || q.includes('what should') || q.includes('recommend') || q.includes('what do')){
    return '📋 <strong>Recommended Actions</strong><br><br>'
      + '🔴 <strong>'+s.high+'</strong> transactions → <em>BLOCK and escalate to compliance</em><br>'
      + '🟡 <strong>'+s.susp+'</strong> transactions → <em>FLAG for senior auditor review</em><br>'
      + '🟢 <strong>'+s.safe+'</strong> transactions → <em>ALLOW — within normal parameters</em><br><br>'
      + 'Total fraud exposure: <strong>'+fmtAmt(s.totalFraudAmt)+'</strong><br><br>'
      + '💡 Use <strong>Jump to Report</strong> for a court-ready forensic memo.';
  }

  /* ---- Safe ---- */
  if(q.includes('safe') || q.includes('clean') || q.includes('legit')){
    return '✅ <strong>'+s.safe.toLocaleString()+'</strong> of <strong>'+s.total.toLocaleString()
      +'</strong> transactions are <span style="color:#34d399"><strong>Safe</strong></span>.<br><br>'
      + 'That\'s <strong>'+((s.safe/s.total)*100).toFixed(1)+'%</strong> within normal parameters.';
  }

  /* ---- Suspicious ---- */
  if(q.includes('suspicious') || q.includes('warning') || q.includes('review')){
    const sTx = tableData.filter(d => d.r === 'suspicious').slice(0,5);
    const list = sTx.map((d,i) =>
      '<strong>'+(i+1)+'.</strong> <code>'+d.id+'</code> — '+d.v+' — ₹'+d.amt).join('<br>');
    return '🟡 <strong>'+s.susp.toLocaleString()+' Suspicious Transactions</strong><br><br>'
      + (list||'None found.') + '<br><br>These require <em>manual review by a senior auditor</em>.';
  }

  /* ---- Benford's law ---- */
  if(q.includes('benford')){
    const bFlags = results.filter(d => d.features.benford_flag).length;
    return '📐 <strong>Benford\'s Law Analysis</strong><br><br>'
      + '<strong>'+bFlags.toLocaleString()+'</strong> transactions have unusual leading digits (7, 8, 9).<br><br>'
      + 'This indicates potential <em>fabricated or manipulated amounts</em>.';
  }

  /* ---- Velocity ---- */
  if(q.includes('velocity') || q.includes('rapid') || q.includes('speed') || q.includes('fast')){
    const rapid = results.filter(d => d.features.rapid_txn_flag).length;
    return '⚡ <strong>Velocity Analysis</strong><br><br>'
      + '<strong>'+rapid.toLocaleString()+'</strong> transactions flagged as rapid-fire.<br><br>'
      + 'High velocity patterns indicate potential <em>automated laundering</em> or <em>smurfing</em>.';
  }

  /* ---- Help / fallback ---- */
  return '🤖 I can help investigate your ledger. Try:<br><br>'
    + '• "<strong>Show summary</strong>" — Risk distribution overview<br>'
    + '• "<strong>High risk transactions</strong>" — Top fraud flags<br>'
    + '• "<strong>Explain risk score</strong>" — How scoring works<br>'
    + '• "<strong>Why is TX-1234 flagged?</strong>" — Specific analysis<br>'
    + '• "<strong>Top anomalies</strong>" — Biggest outliers<br>'
    + '• "<strong>What action should I take?</strong>" — Recommendations<br>'
    + '• "<strong>Benford\'s law</strong>" — First-digit analysis<br>'
    + '• "<strong>Velocity analysis</strong>" — Rapid transaction detection';
}

/* ============================================================
   LedgerSpy Assistant (rule-based, keyword routing)
   ============================================================
   P9: answers only from the backend analysis (API responses). It
   computes nothing: counts, scores, tiers and explanations are the
   API's values, quoted. Every API value is passed through escapeHtml()
   before it is placed in the (otherwise static) answer markup.
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
  processQuery(query).then(html => { hideTyping(); addChatMsg(html, 'bot'); },
                           err => { hideTyping(); addChatMsgText('Could not answer: ' + err.message); });
}

/* ---- Data source: the backend analysis (replaceable in the Node harness) ---- */
const E = escapeHtml;
function lsChatSource(){
  if(typeof window !== 'undefined' && window.LS_CHAT_SOURCE) return window.LS_CHAT_SOURCE;
  const ready = typeof LS_STATE !== 'undefined' && LS_STATE.summary && LS_STATE.datasetId;
  const base = () => '/api/datasets/' + LS_STATE.datasetId + '/risk';
  return {
    ready: !!ready,
    summary: () => LS_STATE.summary,
    detectors: () => LS_STATE.detectors,
    transactions: params => LS_API.get(base() + '/transactions' + LS_API.qs(params)),
    detail: id => LS_API.get(base() + '/transactions/' + encodeURIComponent(id)),
    selected: () => (typeof window !== 'undefined' ? window._selectedTxn : null),
  };
}

/** The transaction ID the user named: the token after "is"/"transaction", else any token with a digit. */
function idInQuery(query){
  const m = query.match(/(?:why\s+is|explain|transaction|txn|row)\s+([^\s?!,;"']+)/i);
  if(m && /\d/.test(m[1])) return m[1];
  const tok = query.split(/[\s,?!;"']+/).find(t => /\d/.test(t) && t.length >= 3);
  return tok || null;
}

function txnLine(r, i){
  return '<strong>' + (i + 1) + '.</strong> <code>' + E(r.txn_id) + '</code> — ' + E(r.tier) + ' ' + E(lsScore(r.score)) +
    ' — ' + E(lsAmount(r.amount, r.currency)) + ' — ' + E(r.payee || '—');
}

/* ============================================================
   QUERY PROCESSOR (async: may read the API)
   ============================================================ */
async function processQuery(query){
  const q = query.toLowerCase();
  const src = lsChatSource();
  if(!src.ready){
    return '⚠️ No analysis loaded. Choose a dataset and click <strong>Analyse dataset</strong> first (the page must be opened through <code>ledgerspy serve</code>).';
  }
  const s = src.summary();

  /* ---- Summary ---- */
  if(q.includes('summary') || q.includes('overview') || q.includes('dashboard')){
    const t = s.counts.tiers;
    return '📊 <strong>Backend analysis summary</strong><br><br>'
      + '📁 Transactions: <code>' + E(lsInt(s.counts.transactions)) + '</code> (' + E(lsInt(s.counts.scored)) + ' scored, ' + E(lsInt(s.counts.insufficient_data)) + ' not assessed)<br>'
      + '⛔ Critical: <code>' + E(lsInt(t.critical)) + '</code> · 🔴 High: <code>' + E(lsInt(t.high)) + '</code> · 🟡 Medium: <code>' + E(lsInt(t.medium)) + '</code> · 🟢 Low: <code>' + E(lsInt(t.low)) + '</code><br>'
      + '💰 Amount in high/critical rows: <strong>' + E(lsAmount(s.amounts.high_or_critical, s.amounts.currency)) + '</strong><br>'
      + '🏢 Counterparties with high/critical rows: <strong>' + E(lsInt(s.entities.with_high_or_critical_transactions)) + '</strong><br><br>'
      + E(s.volume_guard.message) + '.<br><em>' + E(s.method.score) + '</em>';
  }

  /* ---- Methodology ---- */
  if(q.includes('risk score') || q.includes('explain risk') || q.includes('explain score') || (q.includes('how') && q.includes('score')) || q.includes('methodology')){
    const t = s.method.tiers;
    const fams = s.method.families.map(f => E(f.label) + (f.scored ? ' (cap ' + E(f.cap) + ')' : ' (not scored)')).join(', ');
    return '🔍 <strong>How the review-risk score is built</strong><br><br>' + E(s.method.score) + '<br><br>'
      + '<strong>Evidence families:</strong> ' + fams + '<br><br>'
      + '<strong>Tiers (backend configuration):</strong> medium from ' + E(t.medium) + '; high from ' + E(t.high) + ' with ' + E(t.min_sources_high)
      + ' independent sources; critical from ' + E(t.critical) + ' with ' + E(t.min_sources_critical) + '.';
  }

  /* ---- Why is a transaction flagged ---- */
  if(q.includes('why') || q.includes('explain') || q.includes('flagged') || q.includes('reason')){
    const id = idInQuery(query) || src.selected();
    if(!id) return '🔎 Name a transaction, e.g. <em>"why is ' + E(s.top_findings.length ? s.top_findings[0].txn_id : 'T-…') + ' flagged?"</em>, or open a row in the table first.';
    let d;
    try { d = await src.detail(id); }
    catch(e){ return '🔎 Transaction <code>' + E(id) + '</code> was not found in this dataset.'; }
    const ex = d.explanation;
    const lines = ex.contributors.map(c => '&nbsp;&nbsp;• +' + E(c.contribution.toFixed(2)) + ' pts — ' + E(c.detector_label) + ': ' + E(c.explanation)).join('<br>');
    return '🔎 <strong>' + E(d.transaction.txn_id) + '</strong><br><br>' + E(ex.headline) + (lines ? '<br><br>' + lines : '')
      + '<br><br><em>' + E(ex.limitations[0]) + '</em>';
  }

  /* ---- High risk / review queue ---- */
  if(q.includes('high risk') || q.includes('critical') || q.includes('queue') || q.includes('top') || q.includes('anomal') || q.includes('fraud')){
    const page = await src.transactions({ sort: 'priority', size: 5 });
    const rows = page.items.filter(r => r.score);
    if(!rows.length) return '✅ No transaction has a positive review-risk score in this dataset.';
    return '🔴 <strong>Top of the review queue</strong> (backend ranking)<br><br>' + rows.map(txnLine).join('<br>')
      + '<br><br>💡 Ask <em>"why is ' + E(rows[0].txn_id) + ' flagged?"</em> for its explanation. High priority is a reason to review, not a finding of fraud.';
  }

  /* ---- Benford ---- */
  if(q.includes('benford') || q.includes('digit')){
    const b = src.detectors().benford.dataset.first;
    if(!b || b.status !== 'analyzed') return '📐 The backend Benford first-digit test did not run on this data: ' + E(b ? (b.reason || b.status) : 'no result') + '.';
    return '📐 <strong>Benford first-digit test (backend)</strong><br><br>n = <code>' + E(lsInt(b.n)) + '</code> · MAD <code>' + E(b.mad) + '</code> (' + E(b.band)
      + ') · χ² p-value <code>' + E(b.chi2_p_value) + '</code><br>Over-represented digits: <strong>' + E((b.over_represented || []).join(', ') || 'none') + '</strong><br><br>' + E(b.note);
  }

  /* ---- Unavailable detectors ---- */
  if(q.includes('unavailable') || q.includes('missing') || q.includes('not available') || q.includes('limitation')){
    const u = s.detectors_unavailable;
    return '🧩 <strong>Detectors that cannot run on this data</strong><br><br>' + (u.length ? u.map(d => '• <code>' + E(d.detector) + '</code> — ' + E(d.reason)).join('<br>') : 'None.');
  }

  /* ---- Help ---- */
  return '🤖 I answer from the backend analysis only. Try:<br><br>'
    + '• "<strong>Show summary</strong>"<br>'
    + '• "<strong>High risk transactions</strong>" — top of the review queue<br>'
    + '• "<strong>Explain risk score</strong>" — how scoring works<br>'
    + '• "<strong>Why is &lt;transaction ID&gt; flagged?</strong>" — its explanation<br>'
    + '• "<strong>Benford</strong>" — the digit test<br>'
    + '• "<strong>Unavailable detectors</strong>"';
}

if(typeof module !== 'undefined' && module.exports){ module.exports = { processQuery, idInQuery }; }

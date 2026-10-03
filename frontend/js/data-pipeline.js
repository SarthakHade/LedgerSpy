/* ============================================================
   LedgerSpy — CSV loading, transaction table, ledger tools,
   preliminary data-integrity check.
   P1: all data-derived text is rendered with textContent (no
   innerHTML with file content); stable ROW-n IDs; no simulated
   bank reconciliation.
   ============================================================ */
const tableData = [];

/* ---- Table rendering ---- */
const MAX_DISPLAY = 500;   // max rows shown in DOM at once

const TIER_BADGE = {
  high:       { cls:'bg-red',     txt:'HIGH RISK' },
  suspicious: { cls:'bg-amber',   txt:'SUSPICIOUS' },
  safe:       { cls:'bg-green',   txt:'BELOW THRESHOLD' },
  unscored:   { cls:'bg-neutral', txt:'NOT SCORED' },
};

function renderT(){
  const tbody = (typeof document !== 'undefined') ? document.getElementById('tx-body') : null;
  if(!tbody) return;
  const frag = document.createDocumentFragment();
  const total = tableData.length;
  const show  = Math.min(total, MAX_DISPLAY);

  for(let i = 0; i < show; i++){
    const d = tableData[i];
    const badge = TIER_BADGE[d.r] || TIER_BADGE.unscored;
    const ml = (d._mlIdx !== undefined && window._mlResults[d._mlIdx]) ? window._mlResults[d._mlIdx].mlResult : null;

    const mainTr = el('tr', { class:'main-row' }, [
      el('td', null, el('div', { class:'exp-icon', id:'icn-' + i }, '▶')),
      el('td', { class:'tx-id' }, d.id),
      el('td', { style:'font-family:var(--mono);color:var(--txt2)' }, d.date),
      el('td', null, d.v),
      el('td', { style:'font-family:var(--mono);font-weight:500' }, '₹' + d.amt),
      el('td', null, el('span', { class:'badge ' + badge.cls }, badge.txt + (ml ? ' · ' + ml.riskScore : ''))),
    ]);
    mainTr.dataset.r = d.r;
    mainTr.onclick = () => tgRow(i);

    // Detail row: exact heuristic points breakdown for this row
    let breakdown;
    if(ml){
      breakdown = el('div', null, [
        el('div', { style:'font-size:0.8rem;color:var(--txt2);margin-bottom:6px' },
          'Heuristic score ' + ml.riskScore + '/100 (raw ' + ml.rawTotal.toFixed(1) + ' points before the 0–100 cap). ' + ml.action + '.'),
        el('ul', { style:'list-style:none;font-size:0.8rem;color:var(--cyan);line-height:1.6' },
          ml.components.map(c => el('li', null, '+' + c.pts.toFixed(1) + ' pts — ' + c.text))),
      ]);
    } else {
      breakdown = el('div', { style:'font-size:0.85rem;color:var(--txt3)' }, 'Not scored — added after the last scan. Re-run the scan to score it.');
    }
    const detTr = el('tr', { class:'detail-row', id:'det-' + i },
      el('td', { colspan:'6', style:'padding:0;border:none' },
        el('div', { class:'detail-content' }, [
          el('div', null, [ el('div', { class:'detail-h' }, 'Why this row was flagged (heuristic rules)'),
                            el('div', { style:'font-size:0.85rem;color:var(--txt2);line-height:1.5' }, d.rsn) ]),
          el('div', null, [ el('div', { class:'detail-h' }, 'Score breakdown (exact, rule-based preview)'), breakdown ]),
        ])));
    frag.appendChild(mainTr);
    frag.appendChild(detTr);
  }

  setChildren(tbody, []);
  tbody.appendChild(frag);

  if(total > show){
    tbody.appendChild(el('tr', { id:'tx-count-banner' },
      el('td', { colspan:'6', style:'text-align:center;padding:16px;font-family:var(--mono);font-size:0.78rem;color:var(--amber);background:rgba(245,158,11,0.06);border-top:1px solid rgba(245,158,11,0.2)' },
        'Showing first ' + show.toLocaleString() + ' of ' + total.toLocaleString() + ' rows • Use the column filter & search above to narrow results.')));
  }
}
if(typeof window !== 'undefined' && window.addEventListener) window.addEventListener('DOMContentLoaded', renderT);

let tgRow = function(i){
  const r = document.getElementById('det-'+i);
  const ic = document.getElementById('icn-'+i);
  if(!r || !ic) return;
  if(r.style.display==='table-row'){r.style.display='none'; ic.style.transform='rotate(0)'}
  else{r.style.display='table-row'; ic.style.transform='rotate(90deg)'}
  window._selectedTxIdx = i;   // chatbot context
};


/* ============================================================
   CSV STATE & PARSING
   ============================================================ */
let _csvRawHeaders = [];  // actual column names from the CSV
let _csvRawRows = [];     // raw row arrays; index i ↔ data row i+1 (stable)
let _csvDelim = ',';
let _csvFileName = 'ledger.csv';
let _addedRowCount = 0;
let _csvOriginalRowCount = 0;  // rows that came from the file; later rows were added in the UI

function splitCsvLine(line, delim){
  const result = []; let cur = '', inQ = false;
  for(let i = 0; i < line.length; i++){
    const ch = line[i];
    if(ch === '"'){
      if(inQ && line[i+1] === '"'){ cur += '"'; i++; continue; }  // escaped quote ""
      inQ = !inQ; continue;
    }
    if(ch === delim && !inQ){ result.push(cur.trim()); cur=''; continue; }
    cur += ch;
  }
  result.push(cur.trim());
  return result;
}

function parseCsvText(text, filename){
  _csvFileName = filename;
  text = String(text).replace(/^﻿/, '');          // strip UTF-8 BOM
  const firstLine = text.split('\n')[0];
  _csvDelim = firstLine.includes(';') ? ';' : firstLine.includes('\t') ? '\t' : ',';
  const lines = text.trim().split(/\r?\n/).filter(l => l.trim() !== '');
  if(lines.length < 2) return false;
  _csvRawHeaders = splitCsvLine(lines[0], _csvDelim).map(h => h.replace(/^"|"$/g,'').trim());
  _csvRawRows    = lines.slice(1).map(l => splitCsvLine(l, _csvDelim));
  _csvOriginalRowCount = _csvRawRows.length;
  _addedRowCount = 0;
  return true;
}

// Display-field mapping (ID / date / vendor / amount / risk / reason)
const FIELD_ALIASES = {
  id:  ['id','tx_id','txid','transaction_id','transaction id','ref','reference','voucher','voucher_no','voucher no'],
  date:['date','txdate','trans_date','transaction date','date of transaction','date_of_transaction','posting date','posting_date','value date','value_date'],
  v:   ['vendor','payee','description','name','party','vendor name','namedest','beneficiary'],
  amt: ['amount','amount (₹)','amt','value','total','debit','amount(inr)'],
  r:   ['risk','risk_level','risk level'],
  rsn: ['reason','notes','remarks','note','narrative'],
};
function autoMapIdx(){
  const m = {};
  Object.entries(FIELD_ALIASES).forEach(([key, aliases]) => {
    m[key] = _csvRawHeaders.findIndex(h => !lsIsLabelColumn(h) && aliases.some(a => a.toLowerCase() === String(h).toLowerCase()));
  });
  return m;
}
/**
 * Build a table entry. rowNum = 1-based data-row number (stable ID when no ID column).
 * Rows are 'unscored' until the heuristic engine assigns a tier.
 */
function rowToEntry(row, m, rowNum){
  const hasId = m.id >= 0 && row[m.id];
  return {
    id:   hasId ? String(row[m.id])
        : (rowNum > _csvOriginalRowCount ? LS_CONFIG.addedRowIdPrefix : LS_CONFIG.rowIdPrefix) + rowNum,
    date: m.date >= 0 ? (row[m.date] || '—') : '—',
    v:    m.v    >= 0 ? (row[m.v]    || 'Unknown') : 'Unknown',
    amt:  m.amt  >= 0 ? (row[m.amt]  || '0') : '0',
    r:    'unscored',
    rsn:  m.rsn  >= 0 ? (row[m.rsn] || 'Imported via CSV.') : 'Imported via CSV.',
    _rowNum: rowNum,
    _raw: row,
  };
}

/* ============================================================
   Interaction Handlers
   ============================================================ */
let hasRun = false;
function setStatus(t){ setText('main-status-text', t); }

function fileLoaded(input){
  if(!(input.files && input.files[0])) return;
  const file = input.files[0];
  document.getElementById('upload-zone').style.display='none';
  document.getElementById('file-info').style.display='block';
  setText('file-name', file.name);

  hasRun = false;
  window._mlResults = [];
  window._mlSummary = null;
  window._scatterBuilt = false;
  window._barsAnimated = false;
  tableData.length = 0;
  _csvRawHeaders = [];
  _csvRawRows    = [];

  const btn = document.getElementById('run-btn');
  if(btn){
    btn.textContent = 'Execute Deep Scan';
    btn.style.background = '';
    btn.style.opacity = '1';
    btn.style.pointerEvents = 'auto';
  }

  const isCsv = /\.(csv|txt)$/i.test(file.name) || file.type === 'text/csv';
  if(!isCsv){
    setStatus('Unsupported file type. Only .csv is supported in this version (XLSX arrives in roadmap P3).');
    return;
  }
  const reader = new FileReader();
  reader.onload = e => {
    if(parseCsvText(e.target.result, file.name)){
      const labels = _csvRawHeaders.filter(h => lsIsLabelColumn(h));
      setStatus(_csvRawRows.length.toLocaleString() + ' rows detected. Ready for Deep Scan.' +
        (labels.length ? ' Label column(s) ' + labels.join(', ') + ' will be ignored for scoring.' : ''));
      runDataIntegrityCheck(autoMapIdx());
    } else {
      setStatus('Error: could not parse CSV (need a header row and at least one data row).');
    }
  };
  reader.readAsText(file);
}

function launchAudit(){
  if(hasRun) return;
  if(_csvRawRows.length === 0){ setStatus('Please upload a CSV file first.'); return; }
  hasRun = true;
  const btn = document.getElementById('run-btn');
  btn.textContent = 'Scanning…'; btn.style.opacity='0.7'; btn.style.pointerEvents='none';

  const dot = document.getElementById('main-status-dot');
  dot.style.background='var(--cyan)'; dot.style.boxShadow='0 0 10px var(--cyan)';
  setStatus('Computing heuristic features on ' + _csvRawRows.length.toLocaleString() + ' rows…');
  setText('fp-status', 'Scoring…');

  setTimeout(() => {
    runMLOnData();
    setTimeout(() => {
      btn.textContent = 'Scan Complete ✓'; btn.style.background='var(--green)'; btn.style.opacity='1';
      btn.style.pointerEvents = 'auto';
      dot.style.background='var(--amber)'; dot.style.boxShadow='0 0 10px var(--amber)';
      setStatus('Analysis finished — ' + tableData.length.toLocaleString() + ' rows scored by the heuristic preview (rule-based, not a trained model).');
      setText('fp-status', 'Heuristic preview');

      ['fpanel-lock','ae-lock','tv-lock','rp-lock'].forEach(id => {
        const n = document.getElementById(id);
        if(n) n.classList.remove('lock-blur');
      });

      if(window._mlSummary) updateDashboardWithML();

      window._scatterBuilt = false;
      window._barsAnimated = false;
      buildScatter();
      window._scatterBuilt = true;

      ltInit();
      renderT();

      if(typeof addChatMsgText === 'function' && window._mlSummary){
        const s = window._mlSummary;
        addChatMsgText('Scan complete. ' + tableData.length.toLocaleString() + ' rows scored by the heuristic preview: ' +
          s.high + ' high risk, ' + s.susp + ' suspicious, ' + s.safe + ' below thresholds. Average score ' + s.avgRisk + '/100. ' +
          'Try "show summary" or "top anomalies".');
      }
    }, 400);
  }, 800);
}

/* ============================================================
   LEDGER TOOLS — Smart Search, Column Filter, Add Transaction
   ============================================================ */
function ltInit(){
  const addBtn = document.getElementById('open-add-tx');
  if(addBtn) addBtn.style.display = 'inline-flex';
  const sel = document.getElementById('col-filter-select');
  if(sel){
    while(sel.options.length > 1) sel.remove(1);
    const cols = _csvRawHeaders.length > 0 ? _csvRawHeaders : ['TX ID','Date','Vendor','Amount','Risk Level'];
    cols.forEach(col => sel.appendChild(el('option', { value: col }, col)));
  }
  ltBuildForm();
  ltUpdateRowCount();
}

let currentRiskFilter = 'all';

function filt(btn, val){
  currentRiskFilter = val;
  document.querySelectorAll('.t-filter').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('search-in').value = '';
  const pill = document.getElementById('search-count-pill');
  if(pill) pill.style.display = 'none';
  document.querySelectorAll('.main-row').forEach(tr => {
    const pass = val === 'all' || tr.dataset.r === val;
    tr.style.display = pass ? 'table-row' : 'none';
    if(!pass) tr.nextElementSibling.style.display = 'none';
  });
}

function smartSearch(){
  const q    = (document.getElementById('search-in').value || '').toLowerCase().trim();
  const col  = document.getElementById('col-filter-select').value;
  const pill = document.getElementById('search-count-pill');
  const colIdx = col !== 'all' ? _csvRawHeaders.indexOf(col) : -1;

  let visible = 0;
  document.querySelectorAll('.main-row').forEach((tr, idx) => {
    const passRisk = currentRiskFilter === 'all' || tr.dataset.r === currentRiskFilter;
    if(!passRisk){ tr.style.display = 'none'; tr.nextElementSibling.style.display = 'none'; return; }
    if(!q){ tr.style.display = 'table-row'; visible++; return; }
    const d = tableData[idx];
    const haystack = (colIdx >= 0 && d && d._raw) ? String(d._raw[colIdx] || '').toLowerCase() : tr.textContent.toLowerCase();
    const match = haystack.includes(q);
    tr.style.display = match ? 'table-row' : 'none';
    if(!match) tr.nextElementSibling.style.display = 'none';
    else visible++;
  });

  if(q && pill){
    pill.textContent = `${visible} result${visible!==1?'s':''} found`;
    pill.style.display = 'inline-flex';
  } else if(pill){
    pill.style.display = 'none';
  }
}
function searchT(){ smartSearch(); }

function ltFormCols(){ return _csvRawHeaders.length > 0 ? _csvRawHeaders : ['ID','Date','Vendor','Amount','Notes']; }

function ltBuildForm(){
  const grid = document.getElementById('lt-form-grid');
  if(!grid) return;
  setChildren(grid, ltFormCols().map((col, i) => {
    const input = el('input', { type:'text', id:'ltf-' + i, placeholder: col });
    return el('div', { class:'lt-field' }, [ el('label', { for:'ltf-' + i }, col), input ]);
  }));
}

function openAddTx(){
  ltBuildForm();
  ltUpdateRowCount();
  document.getElementById('add-tx-overlay').classList.add('show');
}
function closeAddTx(){
  document.getElementById('add-tx-overlay').classList.remove('show');
}
if(typeof document !== 'undefined' && document.addEventListener){
  document.addEventListener('click', function(e){
    const overlay = document.getElementById('add-tx-overlay');
    if(e.target === overlay) closeAddTx();
  });
}

function ltAddRow(){
  const cols = ltFormCols();
  const rawRow = cols.map((c, i) => { const n = document.getElementById('ltf-' + i); return n ? n.value.trim() : ''; });
  _csvRawRows.push(rawRow);                      // append: existing ROW-n numbers stay stable
  _addedRowCount++;
  const rowNum = _csvRawRows.length;
  const entry = rowToEntry(rawRow, autoMapIdx(), rowNum);   // → NEW-n when the file has no ID column
  entry.rsn = 'Added manually — not scored yet. Re-run the scan to score it.';
  tableData.unshift(entry);                      // show at top until re-scan
  renderT();
  ltUpdateRowCount();
  closeAddTx();
  showImportToast(1);

  // Allow a re-scan so the new row is scored by the same heuristic as every other row.
  hasRun = false;
  const btn = document.getElementById('run-btn');
  if(btn){ btn.textContent = 'Re-run Scan (' + _addedRowCount + ' unscored)'; btn.style.background = ''; btn.style.pointerEvents = 'auto'; }
  setTimeout(() => { const t = document.getElementById('transactions'); if(t) t.scrollIntoView({behavior:'smooth'}); }, 200);
}

function ltUpdateRowCount(){
  setText('lt-row-count', tableData.length + ' rows total in ledger');
}

/** Build the export CSV text (original column order, original row order + added rows). */
function ltBuildCsv(){
  const cols = _csvRawHeaders.length > 0 ? _csvRawHeaders : ltFormCols();
  const esc = v => {
    const s = lsCsvSafeCell(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
  };
  return [cols.map(esc).join(','), ..._csvRawRows.map(r => r.map(esc).join(','))].join('\n');
}

function ltDownloadCsv(){
  const blob = new Blob([ltBuildCsv()], {type:'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = _csvFileName.replace(/\.csv$/i,'') + '_updated.csv';
  a.click();
  URL.revokeObjectURL(url);
}

function showImportToast(n){
  setText('toast-msg', n === 1 ? '1 transaction added to ledger (not scored yet)' : `${n} transactions loaded!`);
  const toast = document.getElementById('import-toast');
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 4000);
}

/* ============================================================
   Preliminary Data Integrity Check (P1)
   Readiness = share of checked cells that are present and well-formed.
   Checks every row (no sampling, no extrapolation). Full suite: roadmap P4.
   ============================================================ */
function runDataIntegrityCheck(m){
  if(_csvRawRows.length === 0) return null;
  const total = _csvRawRows.length;
  let nullAmounts = 0, formatIssues = 0, missingVendors = 0, raggedRows = 0;
  const checked = [];
  if(m.amt >= 0) checked.push('amount ("' + _csvRawHeaders[m.amt] + '")');
  if(m.v   >= 0) checked.push('vendor ("' + _csvRawHeaders[m.v] + '")');

  for(let i = 0; i < total; i++){
    const row = _csvRawRows[i];
    if(row.length !== _csvRawHeaders.length) raggedRows++;
    if(m.amt >= 0){
      const v = row[m.amt];
      if(!v || v.trim() === '') nullAmounts++;
      else if(isNaN(parseFloat(v.replace(/[₹,\s]/g, '')))) formatIssues++;
    }
    if(m.v >= 0 && (!row[m.v] || row[m.v].trim() === '')) missingVendors++;
  }

  const checkedCells = total * ((m.amt >= 0 ? 1 : 0) + (m.v >= 0 ? 1 : 0)) + total;  // + 1 structural check per row
  const issues = nullAmounts + formatIssues + missingVendors + raggedRows;
  // Floor to one decimal so a file with any issue never displays as 100%.
  const score = checkedCells ? (issues === 0 ? 100 : Math.floor(1000 * (1 - issues / checkedCells)) / 10) : 0;
  const result = { total, nullAmounts, formatIssues, missingVendors, raggedRows, issues, score, checked };
  window._integrity = result;

  if(typeof document === 'undefined' || !document.getElementById('di-score-txt')) return result;

  setText('di-score-txt', score + '%');
  const ring = document.getElementById('di-ring');
  const circumference = 2 * Math.PI * 42;
  ring.style.strokeDasharray = circumference;
  ring.style.strokeDashoffset = circumference - (circumference * score / 100);
  ring.style.stroke = score < 80 ? 'var(--red)' : score < 100 ? 'var(--amber)' : 'var(--cyan)';

  setText('di-status-desc',
    score + '% preliminary readiness = share of checked cells that are present and well-formed (' +
    issues.toLocaleString() + ' issue' + (issues === 1 ? '' : 's') + ' in ' + checkedCells.toLocaleString() + ' checks over all ' + total.toLocaleString() + ' rows). ' +
    'Checked: ' + (checked.length ? checked.join(', ') : 'no amount/vendor column recognised') + ', plus column count per row. ' +
    'Duplicate, date and per-column format checks arrive in roadmap P4.');

  const issueBox = document.getElementById('di-issue-list');
  const item = (label, n, color) => el('div', { style:'display:flex;justify-content:space-between;padding:12px 14px;background:var(--bg2);border-radius:6px;border:1px solid var(--border)' }, [
    el('span', { style:'font-size:0.85rem;color:var(--txt2)' }, label),
    el('span', { style:'font-family:var(--mono);color:' + (n ? color : 'var(--green)') }, n.toLocaleString()),
  ]);
  setChildren(issueBox, [
    m.amt >= 0 ? item('Empty amount cells', nullAmounts, 'var(--red)') : item('Amount column not recognised', 1, 'var(--red)'),
    m.amt >= 0 ? item('Non-numeric amount cells', formatIssues, 'var(--amber)') : null,
    m.v   >= 0 ? item('Empty vendor cells', missingVendors, 'var(--amber)') : el('div', { class:'na-inline' }, 'No vendor/payee column recognised by the display mapping — vendor completeness not checked.'),
    item('Rows with wrong number of columns', raggedRows, 'var(--amber)'),
  ].filter(Boolean));
  return result;
}

/* Copy the generated memo as plain text (works offline; falls back to execCommand). */
function copyMemoText(){
  const doc = document.getElementById('print-doc');
  if(!doc) return;
  const text = doc.innerText;
  const done = ok => setText('memo-copy-btn', ok ? '✓ Copied' : 'Copy failed — select the text manually');
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(() => done(true), () => done(fallbackCopy(text)));
  } else done(fallbackCopy(text));
  setTimeout(() => setText('memo-copy-btn', '⧉ Copy Memo Text'), 2500);
}
function fallbackCopy(text){
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
  ta.remove(); return ok;
}

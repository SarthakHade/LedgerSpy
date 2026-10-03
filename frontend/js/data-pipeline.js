/* ============================================================
   Mock Data & Table Initial State
   ============================================================ */
const tableData = [];

/* ---- Table rendering ---- */
const MAX_DISPLAY = 500;   // max rows shown in DOM at once
let _tableDisplayOffset = 0; // for future pagination

function renderT() {
  const tbody = document.getElementById('tx-body');
  if(!tbody) return;
  const frag = document.createDocumentFragment();
  const data  = tableData;
  const total = data.length;
  const show  = Math.min(total, MAX_DISPLAY);

  // Remove old banner if any
  const oldBanner = document.getElementById('tx-count-banner');
  if(oldBanner) oldBanner.remove();

  for(let i = 0; i < show; i++){
    const d = data[i];
    const bg  = d.r==='high' ? 'bg-red' : d.r==='suspicious' ? 'bg-amber' : 'bg-green';
    const txt = d.r==='high' ? 'HIGH RISK' : d.r==='suspicious' ? 'SUSPICIOUS' : 'SAFE';

    const mainTr = document.createElement('tr');
    mainTr.className = 'main-row';
    mainTr.dataset.r  = d.r;
    mainTr.dataset.s  = (d.id + ' ' + d.v).toLowerCase();
    mainTr.onclick = () => tgRow(i);
    mainTr.innerHTML = `
      <td><div class="exp-icon" id="icn-${i}">▶</div></td>
      <td class="tx-id">${d.id}</td>
      <td style="font-family:var(--mono);color:var(--txt2)">${d.date}</td>
      <td>${d.v}</td>
      <td style="font-family:var(--mono);font-weight:500">₹${d.amt}</td>
      <td><span class="badge ${bg}">${txt}</span></td>
    `;

    const detTr = document.createElement('tr');
    detTr.className = 'detail-row';
    detTr.id = 'det-' + i;
    detTr.innerHTML = `
      <td colspan="6" style="padding:0;border:none">
        <div class="detail-content">
          <div>
            <div class="detail-h">AI Flag Reasoning</div>
            <div style="font-size:0.85rem;color:var(--txt2);line-height:1.5">${d.rsn}</div>
          </div>
          <div>
            <div class="detail-h">Pattern Match Info</div>
            <div style="font-size:0.85rem;color:var(--cyan)">
              ${d.r==='safe'?'No negative patterns extracted.':'Algorithm confident match: Behavioral Anomaly Cluster #4'}
            </div>
          </div>
        </div>
      </td>
    `;
    frag.appendChild(mainTr);
    frag.appendChild(detTr);
  }

  tbody.innerHTML = '';
  tbody.appendChild(frag);

  // Show count banner if more rows exist
  if(total > show){
    const banner = document.createElement('tr');
    banner.id = 'tx-count-banner';
    banner.innerHTML = `<td colspan="6" style="text-align:center;padding:16px;font-family:var(--mono);font-size:0.78rem;color:var(--amber);background:rgba(245,158,11,0.06);border-top:1px solid rgba(245,158,11,0.2)">
      Showing first <strong>${show.toLocaleString()}</strong> of <strong>${total.toLocaleString()}</strong> rows &nbsp;&bull;&nbsp;
      Use the column filter &amp; search above to narrow results.
    </td>`;
    tbody.appendChild(banner);
  }
}
// Render initially
window.addEventListener('DOMContentLoaded', renderT);

let tgRow = function(i){
  const r = document.getElementById('det-'+i);
  const ic = document.getElementById('icn-'+i);
  if(!r || !ic) return;
  if(r.style.display==='table-row'){r.style.display='none'; ic.style.transform='rotate(0)'}
  else{r.style.display='table-row'; ic.style.transform='rotate(90deg)'}
  
  // Track selected row for chatbot context
  window._selectedTxIdx = i;
};


/* ============================================================
   CSV STATE & PARSING
   ============================================================ */
let _csvRawHeaders = [];  // actual column names from the CSV
let _csvRawRows = [];     // raw row arrays
let _csvDelim = ',';
let _csvFileName = 'ledger.csv';

function splitCsvLine(line, delim){
  const result = []; let cur = '', inQ = false;
  for(let i = 0; i < line.length; i++){
    const ch = line[i];
    if(ch === '"'){ inQ = !inQ; continue; }
    if(ch === delim && !inQ){ result.push(cur.trim()); cur=''; continue; }
    cur += ch;
  }
  result.push(cur.trim());
  return result;
}

function parseCsvText(text, filename){
  _csvFileName = filename;
  const firstLine = text.split('\n')[0];
  _csvDelim = firstLine.includes(';') ? ';' : firstLine.includes('\t') ? '\t' : ',';
  const lines = text.trim().split('\n').filter(l => l.trim() !== '');
  if(lines.length < 2) return false;
  _csvRawHeaders = splitCsvLine(lines[0], _csvDelim).map(h => h.replace(/^"|"$/g,'').trim());
  _csvRawRows    = lines.slice(1).map(l => splitCsvLine(l, _csvDelim));
  return true;
}

// Map CSV row to tableData entry using auto-detected field mapping
const FIELD_ALIASES = {
  id:  ['id','tx_id','txid','transaction_id','ref','transaction id'],
  date:['date','txdate','trans_date','transaction date'],
  v:   ['vendor','payee','description','name','party','vendor name'],
  amt: ['amount','amount (₹)','amt','value','total','debit','amount(inr)'],
  r:   ['risk','risk_level','status','category','risk level'],
  rsn: ['reason','notes','remarks','note','flag','narrative'],
};
function autoMapIdx(){
  const m = {};
  Object.entries(FIELD_ALIASES).forEach(([key, aliases]) => {
    const idx = _csvRawHeaders.findIndex(h => aliases.some(a => a.toLowerCase() === h.toLowerCase()));
    m[key] = idx;
  });
  return m;
}
function rowToEntry(row, m){
  const riskRaw = m.r >= 0 ? (row[m.r]||'').toLowerCase().trim() : '';
  let risk = 'suspicious';
  if(riskRaw==='high'||riskRaw==='high risk') risk='high';
  else if(riskRaw==='safe'||riskRaw==='low'||riskRaw==='clear') risk='safe';
  return {
    id:   m.id   >= 0 ? (row[m.id]  ||'TX-???') : 'TX-???',
    date: m.date >= 0 ? (row[m.date]||'—')      : '—',
    v:    m.v    >= 0 ? (row[m.v]   ||'Unknown'): 'Unknown',
    amt:  m.amt  >= 0 ? (row[m.amt] ||'0')      : '0',
    r:    risk,
    rsn:  m.rsn  >= 0 ? (row[m.rsn] ||'Imported via CSV.') : 'Imported via CSV.',
    _raw: row,  // keep raw row for CSV export
  };
}

/* ============================================================
   Interaction Handlers
   ============================================================ */
let hasRun = false;
function fileLoaded(input){
  if(input.files && input.files[0]){
    const file = input.files[0];
    document.getElementById('upload-zone').style.display='none';
    document.getElementById('file-info').style.display='block';
    document.getElementById('file-name').textContent = file.name;
    document.getElementById('main-status-text').textContent = 'Ledger selected. Ready for scan.';

    // Reset state for new file
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
      btn.innerHTML = 'Execute Deep Scan';
      btn.style.background = '';
      btn.style.opacity = '1';
      btn.style.pointerEvents = 'auto';
    }

    if(file.name.endsWith('.csv') || file.type === 'text/csv' || file.name.endsWith('.txt')){
      const reader = new FileReader();
      reader.onload = e => {
        const ok = parseCsvText(e.target.result, file.name);
        if(ok){
          document.getElementById('main-status-text').textContent =
            _csvRawRows.length.toLocaleString() + ' rows detected. Ready for Deep Scan.';
          // Do NOT pre-fill tableData here — let ML engine do it
          if(typeof runDataIntegrityCheck === 'function'){
            runDataIntegrityCheck(autoMapIdx());
          }
        } else {
          document.getElementById('main-status-text').textContent = 'Error: Could not parse CSV. Check format.';
        }
      };
      reader.readAsText(file);
    }
  }
}

function launchAudit(){
  if(hasRun) return;
  if(_csvRawRows.length === 0){
    alert('Please upload a CSV file first.');
    return;
  }
  hasRun = true;
  const btn = document.getElementById('run-btn');
  btn.innerHTML = 'Scanning Matrix…'; btn.style.opacity='0.7'; btn.style.pointerEvents='none';

  const dot = document.getElementById('main-status-dot');
  dot.style.background='var(--cyan)'; dot.style.boxShadow='0 0 10px var(--cyan)';
  document.getElementById('main-status-text').textContent = 'Running ML feature engineering on ' + _csvRawRows.length + ' rows…';

  const fps = document.getElementById('fp-status');
  if(fps){ fps.textContent = 'ML Processing…'; fps.style.background='var(--cyan-dim)'; fps.style.color='var(--cyan)'; }

  // Step 1: Feature engineering + scoring (slightly delayed for UI update)
  setTimeout(() => {
    document.getElementById('main-status-text').textContent = 'Running prediction engine…';
    if(typeof runMLOnData === 'function') runMLOnData();

    // Step 2: Dashboard update
    setTimeout(() => {
      btn.innerHTML = 'Scan Complete ✓'; btn.style.background='var(--green)'; btn.style.opacity='1';
      btn.style.pointerEvents = 'auto';
      dot.style.background='var(--red)'; dot.style.boxShadow='0 0 10px var(--red)';
      document.getElementById('main-status-text').textContent =
        'Analysis finished — ' + tableData.length + ' transactions scored by ML engine.';
      if(fps){ fps.textContent = 'Anomalies Detected'; fps.style.background='var(--red-dim)'; fps.style.color='var(--red)'; }

      // Unlock panels
      ['fpanel-lock','ae-lock','tv-lock','rp-lock'].forEach(id => {
        const el = document.getElementById(id);
        if(el) el.classList.remove('lock-blur');
      });

      if(window._mlSummary && typeof updateDashboardWithML === 'function'){
        updateDashboardWithML();
      }

      // Always rebuild scatter and xai bars from real data
      window._scatterBuilt = false;
      window._barsAnimated = false;
      buildScatter();
      window._scatterBuilt = true;

      ltInit();
      renderT();

      if(typeof addChatMsg === 'function'){
        const s = window._mlSummary;
        addChatMsg(
          '✅ <strong>Scan complete!</strong> Analyzed <code>' + tableData.length.toLocaleString() + '</code> transactions.<br><br>' +
          (s ? ('🚨 <strong>' + s.high + ' HIGH-RISK</strong> | ⚠️ ' + s.susp + ' Suspicious | ✅ ' + s.safe + ' Safe<br>Avg Risk Score: <code>' + s.avgRisk + '%</code>') : '') +
          '<br><br>Ask me anything — try <em>"show summary"</em> or <em>"top anomalies"</em>.', 'bot'
        );
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
    cols.forEach(col => {
      const opt = document.createElement('option');
      opt.value = col;
      opt.textContent = col;
      sel.appendChild(opt);
    });
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

  const colToKey = c => {
    const cl = c.toLowerCase();
    if(FIELD_ALIASES.id.some(a => a===cl))   return 'id';
    if(FIELD_ALIASES.date.some(a => a===cl)) return 'date';
    if(FIELD_ALIASES.v.some(a => a===cl))    return 'v';
    if(FIELD_ALIASES.amt.some(a => a===cl))  return 'amt';
    if(FIELD_ALIASES.r.some(a => a===cl))    return 'r';
    if(FIELD_ALIASES.rsn.some(a => a===cl))  return 'rsn';
    return null;
  };
  const fieldKey = col !== 'all' ? colToKey(col) : null;

  let visible = 0;
  const rows = document.querySelectorAll('.main-row');
  rows.forEach((tr, idx) => {
    const passRisk = currentRiskFilter === 'all' || tr.dataset.r === currentRiskFilter;
    if(!passRisk){
      tr.style.display = 'none';
      tr.nextElementSibling.style.display = 'none';
      return;
    }
    if(!q){ tr.style.display = 'table-row'; visible++; return; }
    let haystack = '';
    if(fieldKey && idx < tableData.length){
      haystack = String(tableData[idx][fieldKey] || '').toLowerCase();
    } else {
      haystack = tr.innerText.toLowerCase();
    }
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

function ltBuildForm(){
  const grid = document.getElementById('lt-form-grid');
  if(!grid) return;
  grid.innerHTML = '';
  const cols = _csvRawHeaders.length > 0 ? _csvRawHeaders : ['ID','Date','Vendor','Amount','Risk','Notes'];
  cols.forEach(col => {
    const isRisk = /risk/i.test(col);
    const div = document.createElement('div');
    div.className = 'lt-field';
    const lbl = document.createElement('label');
    lbl.textContent = col;

    let input;
    if(isRisk){
      input = document.createElement('select');
      input.id = 'ltf-' + col;
      ['suspicious','high','safe'].forEach(v => {
        const o = document.createElement('option');
        o.value = v; o.textContent = v.charAt(0).toUpperCase()+v.slice(1);
        input.appendChild(o);
      });
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.id = 'ltf-' + col;
      input.placeholder = col;
    }

    div.appendChild(lbl);
    div.appendChild(input);
    grid.appendChild(div);
  });
}

function ltGetFormValues(){
  const cols = _csvRawHeaders.length > 0 ? _csvRawHeaders : ['ID','Date','Vendor','Amount','Risk','Notes'];
  const row = {};
  cols.forEach(col => {
    const el = document.getElementById('ltf-' + col);
    row[col] = el ? el.value.trim() : '';
  });
  return {row, cols};
}

function openAddTx(){
  ltBuildForm();
  ltUpdateRowCount();
  document.getElementById('add-tx-overlay').classList.add('show');
}
function closeAddTx(){
  document.getElementById('add-tx-overlay').classList.remove('show');
}
document.addEventListener('click', function(e){
  const overlay = document.getElementById('add-tx-overlay');
  if(e.target === overlay) closeAddTx();
});

function ltAddRow(){
  const {row, cols} = ltGetFormValues();
  const rawRow = cols.map(c => row[c] || '');
  const fIdx = key => {
    const aliases = FIELD_ALIASES[key] || [];
    const i = cols.findIndex(c => aliases.some(a => a.toLowerCase()===c.toLowerCase()));
    return i;
  };
  const fm = { id: fIdx('id'), date: fIdx('date'), v: fIdx('v'),
               amt: fIdx('amt'), r: fIdx('r'), rsn: fIdx('rsn') };
  const entry = rowToEntry(rawRow, fm);
  if(entry.id === 'TX-???') entry.id = rawRow[0] || 'TX-NEW';
  if(entry.v  === 'Unknown') entry.v = rawRow[2] || 'New Vendor';
  tableData.unshift(entry); // Adding to top
  _csvRawRows.unshift(rawRow);
  renderT();
  ltUpdateRowCount();
  
  cols.forEach(col => {
    const el = document.getElementById('ltf-' + col);
    if(el && el.tagName === 'INPUT') el.value = '';
  });
  closeAddTx();
  showImportToast(1);
  setTimeout(() => document.getElementById('transactions').scrollIntoView({behavior:'smooth'}), 200);
}

function ltUpdateRowCount(){
  const el = document.getElementById('lt-row-count');
  if(el) el.textContent = tableData.length + ' rows total in ledger';
}

function ltDownloadCsv(){
  const cols = _csvRawHeaders.length > 0 ? _csvRawHeaders : ['ID','Date','Vendor','Amount','Risk','Notes'];
  const m = autoMapIdx();
  const rows = tableData.map(d => {
    if(d._raw && d._raw.length === cols.length) return d._raw;
    return cols.map((col, i) => {
      if(i === m.id)   return d.id;
      if(i === m.date) return d.date;
      if(i === m.v)    return d.v;
      if(i === m.amt)  return d.amt;
      if(i === m.r)    return d.r;
      if(i === m.rsn)  return d.rsn;
      return '';
    });
  });

  const escape = v => (String(v).includes(',') || String(v).includes('"'))
    ? `"${String(v).replace(/"/g,'""')}"` : String(v);

  const csvContent = [
    cols.map(escape).join(','),
    ...rows.map(r => r.map(escape).join(','))
  ].join('\n');

  const blob = new Blob([csvContent], {type:'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = _csvFileName.replace('.csv','') + '_updated.csv';
  a.click();
  URL.revokeObjectURL(url);
}

function showImportToast(n){
  const toast = document.getElementById('import-toast');
  document.getElementById('toast-msg').textContent =
    n === 1 ? '1 transaction added to ledger!' : `${n} transactions loaded!`;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 4000);
}

/* ============================================================
   HACKATHON REQUIREMENT: Data Integrity Dashboard
   ============================================================ */
function runDataIntegrityCheck(m) {
  if (_csvRawRows.length === 0) return;
  const total = _csvRawRows.length;
  
  let nullAmounts = 0;
  let missingVendors = 0;
  let formatIssues = 0;

  const sampleSize = Math.min(total, 5000); 
  const step = Math.max(1, Math.floor(total / sampleSize));
  
  for(let i=0; i<total; i+=step) {
    const row = _csvRawRows[i];
    if(m.amt >= 0) {
      const v = row[m.amt];
      if(!v || v.trim() === '') nullAmounts++;
      else if(isNaN(parseFloat(v.replace(/[₹,\s]/g, '')))) formatIssues++;
    }
    if(m.v >= 0) {
      if(!row[m.v] || row[m.v].trim() === '') missingVendors++;
    }
  }
  
  nullAmounts = Math.round(nullAmounts * (total/sampleSize));
  missingVendors = Math.round(missingVendors * (total/sampleSize));
  formatIssues = Math.round(formatIssues * (total/sampleSize));
  
  const totalIssues = nullAmounts + missingVendors + formatIssues;
  const score = Math.max(60, Math.round(100 - ((totalIssues / total) * 100) * 10)); 
  const finalScore = totalIssues === 0 ? 100 : score;

  document.getElementById('di-score-txt').textContent = finalScore + '%';
  const ring = document.getElementById('di-ring');
  const circumference = 2 * Math.PI * 42; 
  ring.style.strokeDasharray = circumference;
  ring.style.strokeDashoffset = circumference - (circumference * finalScore / 100);
  
  if (finalScore < 100) ring.style.stroke = 'var(--amber)';
  if (finalScore < 80)  ring.style.stroke = 'var(--red)';
  
  document.getElementById('di-status-desc').innerHTML = `<strong>${finalScore}%</strong> data readiness. ${totalIssues > 0 ? `Detected ${totalIssues.toLocaleString()} anomalies in structure that may affect ML predictions.` : 'Data schema perfectly maps to the LedgerSpy engine.'}`;
  
  const issueBox = document.getElementById('di-issue-list');
  if(totalIssues === 0) {
    issueBox.innerHTML = `<div style="padding:16px;background:var(--bg2);border-radius:8px;border:1px solid rgba(52,211,153,0.3);color:var(--green);font-size:0.85rem;display:flex;align-items:center;gap:10px">✓ No schema malformations detected. Null check passed.</div>`;
    document.getElementById('di-clean-btn').disabled = true;
  } else {
    document.getElementById('di-clean-btn').disabled = false;
    issueBox.innerHTML = `
      ${nullAmounts > 0 ? `<div style="display:flex;justify-content:space-between;padding:12px 14px;background:rgba(248,113,113,0.08);border-radius:6px;border:1px solid rgba(248,113,113,0.2)"><span style="font-size:0.82rem;font-family:var(--mono);color:var(--red)">NULL AMOUNTS</span><span style="font-family:var(--mono);font-size:0.82rem">${nullAmounts.toLocaleString()} rows</span></div>` : ''}
      ${missingVendors > 0 ? `<div style="display:flex;justify-content:space-between;padding:12px 14px;background:rgba(245,158,11,0.08);border-radius:6px;border:1px solid rgba(245,158,11,0.2)"><span style="font-size:0.82rem;font-family:var(--mono);color:var(--amber)">MISSING ENTITY NAMES</span><span style="font-family:var(--mono);font-size:0.82rem">${missingVendors.toLocaleString()} rows</span></div>` : ''}
      ${formatIssues > 0 ? `<div style="display:flex;justify-content:space-between;padding:12px 14px;background:rgba(245,158,11,0.08);border-radius:6px;border:1px solid rgba(245,158,11,0.2)"><span style="font-size:0.82rem;font-family:var(--mono);color:var(--amber)">UNPARSABLE CURRENCY (VAR_CHAR)</span><span style="font-family:var(--mono);font-size:0.82rem">${formatIssues.toLocaleString()} rows</span></div>` : ''}
    `;
  }
}

/* ============================================================
   HACKATHON REQUIREMENT: Bank Statement Reconciliation
   ============================================================ */
function runMockBSR() {
  const btn = document.getElementById('btn-run-recon');
  btn.disabled = true;
  btn.innerText = 'Matching...';
  
  setTimeout(() => {
    btn.innerText = 'Recon Complete';
    btn.classList.replace('btn-primary', 'btn-secondary');
    
    const sample = tableData.slice(0, 50);
    document.getElementById('bsr-l-count').textContent = sample.length + ' rows loaded';
    document.getElementById('bsr-b-count').textContent = sample.length + ' rows mapped';
    
    const lBody = document.getElementById('bsr-ledger-body');
    const bBody = document.getElementById('bsr-bank-body');
    lBody.innerHTML = '';
    bBody.innerHTML = '';
    
    sample.forEach((d, i) => {
      const ltr = document.createElement('tr');
      ltr.innerHTML = `<td>${d.id}</td><td>${d.v}</td><td style="font-family:var(--mono)">₹${d.amt}</td>`;
      lBody.appendChild(ltr);
      
      const rand = Math.random();
      const btr = document.createElement('tr');
      
      if(rand > 0.15) { 
        btr.innerHTML = `<td><span style="color:var(--green);font-size:0.7rem;font-family:var(--mono)">✓ EXACT</span></td><td>${d.v.toUpperCase()} INC</td><td style="font-family:var(--mono)">₹${d.amt}</td>`;
      } else if (rand > 0.05) { 
        const fakeAmt = Math.round(parseFloat(d.amt.replace(/,/g,'')) * 0.9);
        btr.innerHTML = `<td><span style="color:var(--amber);font-size:0.7rem;font-family:var(--mono)">⚠ VALUE DIFF</span></td><td>${d.v.toUpperCase()} INC</td><td style="font-family:var(--mono);color:var(--amber)">₹${fakeAmt.toLocaleString()}</td>`;
      } else { 
        btr.innerHTML = `<td><span style="color:var(--red);font-size:0.7rem;font-family:var(--mono)">❌ NOT IN STATEMENT</span></td><td style="color:var(--txt3)">—</td><td style="color:var(--txt3)">—</td>`;
      }
      bBody.appendChild(btr);
    });
    
  }, 1200);
}

/* ============================================================
   LedgerSpy — P14 bank statement reconciliation view
   ============================================================
   Boundary: statuses, counts, amounts, differences, evidence scores,
   duplicates, balance checks and findings come from /api/reconciliation…
   only. The page computes nothing: it lists datasets, sends the analyst's
   confirmations (bank mapping semantics, scopes, ledger debit meaning) and
   renders the backend's results. Amounts are integer minor units turned
   into exact decimal text. All data-derived text is set with textContent.
   P14b: PDF statements show the backend's extraction (per-page method,
   coverage, checks, review-required rows); the reviewer confirms or
   excludes rows, and the original page is drawn on a canvas. Nothing is
   parsed or extracted in the browser.
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('bsr-app')) return;

  const $ = id => document.getElementById(id);
  const STATUS_LABEL = {
    exact_match: 'Exact match', probable_match: 'Probable match', grouped_match: 'Grouped match', partial_match: 'Partial match',
    ambiguous: 'Ambiguous', duplicate_candidate: 'Duplicate candidate', unmatched_ledger: 'Unmatched ledger', unmatched_bank: 'Unmatched bank',
    insufficient_data: 'Insufficient data', not_applicable: 'Not applicable',
  };
  const STATUS_CLASS = {
    exact_match: 'bg-green', probable_match: 'bg-green', grouped_match: 'bg-green', partial_match: 'bg-amber', ambiguous: 'bg-amber',
    duplicate_candidate: 'bg-red', unmatched_ledger: 'bg-red', unmatched_bank: 'bg-red', insufficient_data: 'bg-neutral', not_applicable: 'bg-neutral',
  };
  const ST = { all: [], datasets: [], ledger: '', bank: '', pdfDpi: 110, ocr: null, view: 0, setup: null, run: null, filters: { status: '', account: '', currency: '', from: '', to: '', q: '' }, page: 1, size: 50 };

  function minor(m, currency){
    if(m === null || m === undefined) return '—';
    const neg = m < 0, a = Math.abs(m);
    return (neg ? '-' : '') + lsAmount(Math.floor(a / 100) + '.' + String(a % 100).padStart(2, '0'), currency);
  }
  function perCurrency(obj){
    const e = Object.entries(obj || {});
    return e.length ? e.map(([c, v]) => minor(v, c === 'unknown' ? null : c)).join(' + ') : '—';
  }
  function badge(status){ return el('span', { class: 'badge ' + (STATUS_CLASS[status] || 'bg-neutral') }, STATUS_LABEL[status] || status); }
  function muted(t){ return el('div', { class: 'ls-muted' }, t); }
  function na(t){ return el('div', { class: 'na-inline' }, t); }
  function button(label, onClick, primary, id){
    const b = el('button', { class: primary ? 'btn btn-primary' : 'btn btn-outline', type: 'button', id: id || null }, label);
    b.addEventListener('click', onClick);
    return b;
  }
  function select(id, options, value, label){
    const s = el('select', { class: 'col-filter-select', id: id, 'aria-label': label || id }, options.map(([v, t]) => el('option', { value: v }, t)));
    s.value = value || '';
    return s;
  }
  function table(headers, rows, onRow){
    return el('div', { class: 'tx-table-wrap di-table' }, el('table', { class: 'tx-table' }, [
      el('thead', null, el('tr', null, headers.map(h => el('th', null, h)))),
      el('tbody', null, rows.length ? rows.map((r, i) => {
        const tr = el('tr', onRow ? { class: 'main-row' } : null, r.map(c => el('td', null, c instanceof Node ? c : (c === null || c === undefined ? '—' : String(c)))));
        if(onRow) tr.addEventListener('click', () => onRow(i));
        return tr;
      }) : el('tr', null, el('td', { colspan: String(headers.length) }, 'None.'))),
    ]));
  }
  function kv(obj){
    const dl = el('dl', { class: 'ls-kv' });
    Object.entries(obj).forEach(([k, v]) => { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v === null || v === undefined ? '—' : String(v))); });
    return dl;
  }
  function errText(e){ return e.message + (e.details ? ' ' + JSON.stringify(e.details) : ''); }

  if(!LS_API.available){
    setChildren($('bsr-setup'), na('Not available: reconciliation runs in the local LedgerSpy service (`ledgerspy serve`). Nothing is matched in the browser.'));
    return;
  }

  /* ---------------- datasets ---------------- */
  async function loadDatasets(){
    try { ST.all = (await LS_API.get('/api/datasets')).items; ST.datasets = ST.all.filter(d => d.status === 'canonicalised'); }
    catch(e){ setChildren($('bsr-setup'), na('Could not list datasets: ' + e.message)); return; }
    renderPickers();
  }
  function renderPickers(){
    const ledgers = ST.datasets.filter(d => (d.kind || 'ledger') === 'ledger');
    if(!ST.ledger && ledgers.length) ST.ledger = ledgers[0].dataset_id;
    const caseId = (ST.datasets.find(d => d.dataset_id === ST.ledger) || {}).case_id;
    const banks = ST.datasets.filter(d => d.kind === 'bank_statement' && d.case_id === caseId);
    if(!banks.some(b => b.dataset_id === ST.bank)) ST.bank = banks.length ? banks[0].dataset_id : '';
    const ls = select('bsr-ledger', [['', ledgers.length ? 'choose a ledger…' : 'no ledger imported']].concat(ledgers.map(d => [d.dataset_id, d.original_filename + ' · ' + lsInt(d.row_count) + ' rows'])), ST.ledger, 'Ledger dataset');
    const bs = select('bsr-bank', [['', banks.length ? 'choose a statement…' : 'no bank statement in this case']].concat(banks.map(d => [d.dataset_id, d.original_filename + ' · ' + lsInt(d.row_count) + ' lines'])), ST.bank, 'Bank statement dataset');
    ls.addEventListener('change', () => { ST.ledger = ls.value; ST.bank = ''; renderPickers(); });
    bs.addEventListener('change', () => { ST.bank = bs.value; loadSetup(); });
    const file = el('input', { type: 'file', id: 'bsr-file', accept: '.csv,.xlsx,.pdf', style: 'display:none' });
    const pending = ST.all.filter(d => d.kind === 'bank_statement' && d.case_id === caseId && d.status === 'mapping_required');
    file.addEventListener('change', () => { if(file.files[0]) importStatement(file.files[0]); file.value = ''; });
    setChildren($('bsr-pickers'), [
      el('div', { class: 'di-toolbar' }, [muted('Ledger'), ls, muted('Bank statement'), bs,
        button('Import bank statement (CSV / XLSX / PDF)', () => { if(!ST.ledger){ say('Choose the ledger first: the statement is imported into its case.'); return; } file.click(); }, false, 'bsr-import'), file]),
      pending.length ? el('div', { class: 'di-toolbar' }, [muted('Statements awaiting review / mapping:')].concat(pending.map(d =>
        button(d.original_filename + ' (' + d.format + ')', () => resume(d.dataset_id), false)))) : null,
    ].filter(Boolean));
    if(ST.ledger && ST.bank) loadSetup(); else setChildren($('bsr-setup'), na(ST.ledger ? 'Import or choose a bank statement of the same case to reconcile.' : 'Import a ledger first (top of the page).'));
  }
  function say(t){ setText('bsr-msg', t); }

  /* ---------------- import: upload → confirm bank semantics → integrity ---------------- */
  async function importStatement(f){
    const caseId = (ST.datasets.find(d => d.dataset_id === ST.ledger) || {}).case_id;
    say('Uploading ' + f.name + ' as a bank statement…');
    if(/\.pdf$/i.test(f.name)) say('Uploading ' + f.name + ': the local service extracts the text (and runs OCR on scanned pages); this can take a while…');
    try {
      const ds = await LS_API.upload(caseId, f, 'bank_statement');
      if(ds.evidence.format === 'pdf') showPdfReview(ds.dataset_id); else showMapping(ds);
    } catch(e){ say('Import refused: ' + errText(e)); }
  }
  async function resume(id){
    const ds = await LS_API.get('/api/datasets/' + id);
    if(ds.evidence.format === 'pdf') showPdfReview(id); else showMapping(ds);
  }

  /* ---------------- P14b: PDF extraction review (backend data only) ---------------- */
  async function showPdfReview(id, filter){
    const my = ++ST.view;
    filter = filter || 'required';
    let s, page;
    try {
      s = await LS_API.get('/api/datasets/' + id + '/pdf');
      const q = filter === 'all' ? {} : filter === 'transactions' ? { kind: 'transaction' } : filter === 'unparsed_kind' ? { kind: 'unparsed' } : { review: filter };
      page = await LS_API.get('/api/datasets/' + id + '/pdf/items' + LS_API.qs(Object.assign({ size: 200 }, q)));
    } catch(e){ if(my === ST.view) setChildren($('bsr-setup'), na('Extraction not available: ' + errText(e))); return; }
    if(my !== ST.view) return;
    const x = s.extraction, ocr = x.engine.ocr || {};
    const pages = s.pages.map(p => [p.page, p.method, lsInt(p.text_chars), lsShare(p.image_coverage, 0), p.ocr_mean_confidence === undefined || p.ocr_mean_confidence === null ? '—' : String(p.ocr_mean_confidence),
      lsInt(p.lines), (p.reason ? p.reason + ' ' : '') + (p.warnings || []).join('; ')]);
    const fsel = select('bsr-pdf-filter', [['required', 'review-required'], ['pending', 'pending decision'], ['transactions', 'all transactions'], ['unparsed', 'unparsed lines'], ['all', 'every line']], filter, 'Rows to show');
    fsel.addEventListener('change', () => showPdfReview(id, fsel.value === 'unparsed' ? 'unparsed_kind' : fsel.value));
    const canvas = el('canvas', { id: 'bsr-pdf-canvas', width: '10', height: '10', style: 'max-width:100%;border:1px solid var(--border);display:none' });
    const rows = page.items.map(it => {
      const acts = [];
      if(it.review_required && s.data_state !== 'confirmed'){
        if(it.kind === 'transaction') acts.push(button('Confirm', () => decide(id, it.item_id, 'confirm', filter), false));
        acts.push(button(it.kind === 'transaction' ? 'Exclude' : 'Not a transaction', () => decide(id, it.item_id, 'exclude', filter), false));
      }
      acts.push(button('Page ' + it.page, () => drawPage(id, it.page, it.bbox, canvas), false));
      const fields = it.fields ? Object.entries(it.fields).map(([k, v]) => k + ': ' + v).join(' · ') : (it.reason || '');
      return [it.page, it.kind, it.method, it.text, fields, (it.reasons || []).join(', ') || '—', it.review_state, el('span', null, acts)];
    });
    const checks = s.statement.checks || {};
    setChildren($('bsr-setup'), [
      el('h4', null, 'PDF extraction review'),
      muted('Evidence ' + s.evidence.original_filename + ' · sha256 ' + s.evidence.sha256.slice(0, 16) + '… (stored unchanged) · extraction ' + x.extraction_id + ' · status ' + x.status
        + (x.not_processed_pages.length ? ' · pages NOT processed: ' + x.not_processed_pages.join(', ') : '')),
      muted('PDF text: PDFium ' + x.engine.pdf.pdfium + ' (pypdfium2 ' + x.engine.pdf.pypdfium2 + ') · OCR: ' + (ocr.used === false ? 'not needed (' + ocr.reason + ')'
        : ocr.available ? 'Tesseract ' + ocr.version + ', languages ' + (ocr.languages || []).join('+') + ', ' + ocr.dpi + ' dpi' : 'UNAVAILABLE — ' + ocr.reason)),
      x.warnings.length ? el('div', { class: 'na-card' }, x.warnings.join(' ')) : null,
      table(['Page', 'Method', 'Text chars', 'Image coverage', 'OCR mean conf.', 'Lines', 'Notes'], pages),
      muted('Lines by kind: ' + Object.entries(s.coverage.by_kind).map(([k, n]) => k + ' ' + n).join(', ') + ' · transactions ' + s.coverage.transactions + ' · review-required ' + s.coverage.review_required),
      muted('Statement checks (from the PDF): ' + (s.statement.checks.status === 'evaluated'
        ? 'opening ' + (checks.checks.opening_matches === null ? 'not stated' : checks.checks.opening_matches ? 'matches' : 'DIFFERS') + ', closing ' + (checks.checks.closing_matches === null ? 'not stated' : checks.checks.closing_matches ? 'matches' : 'DIFFERS') + ', balance breaks ' + checks.breaks.length
        : 'not evaluated — ' + s.statement.checks.reason)),
      muted('Data states — extracted: ' + s.states.extracted + ' · reviewed: ' + s.states.reviewed + ' · confirmed: ' + s.states.confirmed + '. Current: ' + s.data_state + '. Review: ' + Object.entries(s.review.by_state).map(([k, n]) => k + ' ' + n).join(', ') + '.'),
      muted(s.note),
      el('div', { class: 'di-toolbar', style: 'margin-top:8px' }, [muted('Show'), fsel,
        button('Re-run extraction', async () => { say('Re-running the extraction…'); try { await LS_API.post('/api/datasets/' + id + '/pdf/extract'); say(''); showPdfReview(id, filter); } catch(e){ say('Not re-run: ' + errText(e)); } }, false, 'bsr-pdf-reextract'),
        button('Continue to the statement mapping', async () => { if(!s.review.can_confirm){ say('Not yet: the extraction must be complete and every review-required row decided.'); return; } showMapping(await LS_API.get('/api/datasets/' + id)); }, true, 'bsr-pdf-continue')]),
      el('div', { id: 'bsr-pdf-items' }, table(['Page', 'Kind', 'Method', 'Source text', 'Extracted fields', 'Review reasons', 'State', ''], rows)),
      page.total > page.items.length ? muted('Showing ' + page.items.length + ' of ' + lsInt(page.total) + ' lines.') : null,
      canvas,
    ].filter(Boolean));
    say('Review the extracted rows against the PDF page before confirming.');
  }
  async function decide(id, itemId, decision, filter){
    try { await LS_API.post('/api/datasets/' + id + '/pdf/review', { decisions: [{ item_id: itemId, decision: decision, reason: 'reviewed in the Bank Recon tab' }] }); showPdfReview(id, filter); }
    catch(e){ say('Decision refused: ' + errText(e)); }
  }
  async function drawPage(id, pageNo, bbox, canvas){
    try {
      const bmp = await createImageBitmap(await LS_API.blob('/api/datasets/' + id + '/pdf/pages/' + pageNo + '/image' + LS_API.qs({ dpi: ST.pdfDpi })));
      canvas.width = bmp.width; canvas.height = bmp.height; canvas.style.display = 'block';
      const ctx = canvas.getContext('2d');
      ctx.drawImage(bmp, 0, 0);
      if(bbox){ const k = ST.pdfDpi / 72; ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 2; ctx.strokeRect(bbox[0] * k - 3, bbox[1] * k - 3, (bbox[2] - bbox[0]) * k + 6, (bbox[3] - bbox[1]) * k + 6); }
      canvas.scrollIntoView({ block: 'nearest' });
    } catch(e){ say('Page image not available: ' + errText(e)); }
  }
  function showMapping(ds){
    ++ST.view;
    const ms = ds.mapping_suggestion, proposed = JSON.parse(JSON.stringify(ms.proposed_mapping));
    proposed.bank = proposed.bank || {};
    const sem = (ms.bank || {}).direction_semantics || {};
    const parts = [
      el('h4', null, 'Confirm the bank statement mapping'),
      muted(ds.evidence.original_filename + ' · ' + lsInt(ds.row_count) + ' lines · evidence sha256 ' + ds.evidence.sha256.slice(0, 16) + '…'),
      table(['Canonical field', 'Source column'], Object.entries(proposed.fields).sort()),
      muted('Direction semantics: ' + (sem.status || '—') + (sem.basis ? ' — ' + sem.basis : '')),
      muted('Running balance column: ' + (proposed.bank.balance_column || 'none (balance checks unavailable)')),
    ];
    const inputs = {};
    ms.requires_confirmation.forEach(item => {
      parts.push(el('div', { class: 'na-card', style: 'margin-top:8px' }, item.item + ': ' + item.reason));
      if(item.item === 'bank.debit_means') inputs.debit = select('bsr-debit-means', [['', 'choose…'], ['outflow', 'a debit is money OUT of the account'], ['inflow', 'a debit is money INTO the account']], '', 'Debit meaning');
      if(item.item === 'bank.amount_sign') inputs.sign = select('bsr-amount-sign', [['', 'choose…'], ['positive_inflow', 'positive = money in'], ['positive_outflow', 'positive = money out']], '', 'Amount sign');
      if(item.item === 'currency') inputs.currency = el('input', { type: 'text', id: 'bsr-currency', maxlength: '3', placeholder: 'ISO code stated on the statement (optional)', class: 'col-filter-select' });
      if(item.item === 'options.date_format' && item.candidates) inputs.dateFormat = select('bsr-date-format', [['', 'choose the date format…']].concat(item.candidates.map(c => [c, c])), '', 'Date format');
      if(item.item === 'options.decimal_separator') inputs.decimal = select('bsr-decimal', [['', 'choose the decimal separator…'], ['.', 'point (1,234.56)'], [',', 'comma (1.234,56)']], '', 'Decimal separator');
    });
    Object.values(inputs).forEach(n => parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:6px' }, [n])));
    const msg = el('div', { class: 'ls-muted', id: 'bsr-map-msg', style: 'margin-top:8px' });
    parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:12px' }, [
      button('Confirm statement mapping', async () => {
        if(inputs.debit){ if(!inputs.debit.value){ msg.textContent = 'Confirm what a debit means first.'; return; } proposed.bank.debit_means = inputs.debit.value; }
        if(inputs.sign){ if(!inputs.sign.value){ msg.textContent = 'Confirm the amount sign first.'; return; } proposed.bank.amount_sign = inputs.sign.value; }
        if(inputs.currency && inputs.currency.value.trim()) proposed.options.currency = inputs.currency.value.trim().toUpperCase();
        if(inputs.dateFormat){ if(!inputs.dateFormat.value){ msg.textContent = 'Choose the date format first.'; return; } proposed.options.date_format = inputs.dateFormat.value; }
        if(inputs.decimal){ if(!inputs.decimal.value){ msg.textContent = 'Choose the decimal separator first.'; return; } proposed.options.decimal_separator = inputs.decimal.value; }
        try {
          msg.textContent = 'Confirming…';
          await LS_API.put('/api/datasets/' + ds.dataset_id + '/mapping', proposed);
          await LS_API.post('/api/datasets/' + ds.dataset_id + '/integrity');
          ST.bank = ds.dataset_id;
          await loadDatasets();
        } catch(e){ msg.textContent = 'Not accepted: ' + errText(e); }
      }, true, 'bsr-confirm-mapping'),
    ]));
    parts.push(msg);
    setChildren($('bsr-setup'), parts);
    say('Check the statement mapping and confirm the direction semantics.');
  }

  /* ---------------- setup: scopes + ledger debit meaning ---------------- */
  async function loadSetup(){
    const my = ++ST.view;  // a slower, older render never replaces a newer panel (e.g. a PDF review)
    setChildren($('bsr-setup'), na('Loading what must be confirmed…'));
    let setup;
    try { setup = await LS_API.get('/api/reconciliation/setup' + LS_API.qs({ ledger_dataset_id: ST.ledger, bank_dataset_id: ST.bank })); }
    catch(e){ if(my === ST.view) setChildren($('bsr-setup'), na('Setup not available: ' + errText(e))); return; }
    if(my !== ST.view) return;
    ST.setup = setup;
    const s = ST.setup, props = s.proposals.scopes;
    const cols = [['', 'all ledger rows']].concat(s.ledger.scope_columns.map(c => [c, c]));
    const rows = s.bank.accounts.map(a => {
      const p = props.find(x => x.account_key === a.account_key) || {};
      const col = select('bsr-scope-col-' + a.account_key, cols, p.ledger_column || '', 'Ledger column for ' + a.account);
      const val = el('input', { type: 'text', class: 'col-filter-select', id: 'bsr-scope-val-' + a.account_key, value: p.ledger_value || '', placeholder: 'value' });
      const use = el('input', { type: 'checkbox', id: 'bsr-scope-use-' + a.account_key });
      use.checked = !!p.account_key || s.bank.accounts.length === 1;
      return { a, col, val, use, row: [use, a.account, lsInt(a.rows), Object.entries(a.currencies).map(([c, n]) => c + ' ' + lsInt(n)).join(', '), (a.first_date || '—') + ' … ' + (a.last_date || '—'), col, val, p.basis || '—'] };
    });
    const dirProp = s.proposals.ledger_debit_means;
    const needDir = s.requires_confirmation.some(r => r.item === 'ledger_debit_means');
    const dir = needDir ? select('bsr-ledger-debit', [['', 'choose…'], ['outflow', 'a ledger debit is money OUT of the bank account'], ['inflow', 'a ledger debit is money INTO the bank account']], dirProp ? dirProp.value : '', 'Ledger debit meaning') : null;
    const parts = [
      el('h4', null, 'Scope: which ledger rows belong to which bank account'),
      muted('Rows are only compared inside a scope, a currency and a direction. Proposals come from evidence (group-company master) and must be confirmed.'),
      table(['Use', 'Account', 'Lines', 'Currencies', 'Period', 'Ledger column', 'Value', 'Basis of proposal'], rows.map(r => r.row)),
      needDir ? el('div', { class: 'di-toolbar', style: 'margin-top:10px' }, [muted('Ledger debit/credit meaning'), dir]) : null,
      needDir ? muted(dirProp ? 'Proposal: ' + dirProp.basis + '. Confirm or change it.' : 'No proposal: the ledger does not show who holds the account; choose explicitly.') : null,
      s.limitations.length ? muted('Limitations: ' + s.limitations.join(' ')) : null,
      muted('Readiness (P4): ledger ' + (s.readiness.ledger ? s.readiness.ledger.status : 'no integrity run') + ' · bank ' + (s.readiness.bank ? s.readiness.bank.status : 'no integrity run')),
      el('div', { class: 'di-toolbar', style: 'margin-top:12px' }, [button('Run reconciliation', async () => {
        const scopes = rows.filter(r => r.use.checked).map(r => r.col.value ? { account_key: r.a.account_key, ledger_column: r.col.value, ledger_value: r.val.value.trim() } : { account_key: r.a.account_key });
        if(!scopes.length){ say('Select at least one account.'); return; }
        if(dir && !dir.value){ say('Confirm what a ledger debit means first.'); return; }
        say('Reconciling in the local service…');
        try {
          ST.run = await LS_API.post('/api/reconciliation/runs', { ledger_dataset_id: ST.ledger, bank_dataset_id: ST.bank, scopes, ledger_debit_means: dir ? dir.value : null });
          ST.page = 1; say('');
          renderRun();
        } catch(e){ say('Not run: ' + errText(e)); }
      }, true, 'bsr-run')]),
    ];
    setChildren($('bsr-setup'), parts.filter(Boolean));
  }

  /* ---------------- run ---------------- */
  function renderRun(){
    const r = ST.run, s = r.summary;
    const cards = Object.keys(STATUS_LABEL).map(k => el('div', { class: 'bsr-stat', 'data-status': k }, [badge(k), el('div', { class: 'bsr-stat-n' }, lsInt(s.results_by_status[k]))]));
    const amt = side => [side, perCurrency(s.matched_amount_minor[side]), perCurrency(s.partial_amount_minor[side]), perCurrency(s.ambiguous_amount_minor[side]), perCurrency(s.unmatched_amount_minor[side]),
      lsInt(s.matched_rows[side]) + ' of ' + lsInt(s.eligible_rows[side]) + ' (' + lsShare(s.matched_row_share[side]) + ')'];
    setChildren($('bsr-summary'), [
      muted('Run ' + r.run_id + ' · result sha256 ' + r.result_sha256.slice(0, 16) + '… · ' + lsInt(s.ledger_rows) + ' ledger rows, ' + lsInt(s.bank_rows) + ' bank lines'),
      el('div', { class: 'bsr-stats' }, cards),
      table(['Side', 'Matched', 'Partial (review)', 'Ambiguous', 'Unmatched', 'Matched rows'], [amt('ledger'), amt('bank')]),
      muted(s.currency_note + ' ' + s.matched_row_share_note),
      muted('Scopes: ' + r.scopes.map(x => x.account + (x.ledger_filter ? ' ↔ ' + x.ledger_filter.column + ' = ' + x.ledger_filter.value : ' ↔ all ledger rows') + (x.confirmed ? '' : ' (not confirmed)')).join('; ')),
      muted('Balance check: ' + s.balance_check.status + (s.balance_check.reason ? ' — ' + s.balance_check.reason : '') + ' · duplicate candidate groups: ' + lsInt(s.duplicates.groups)),
      r.truncation.limits_reached ? el('div', { class: 'na-card' }, 'Search limits reached for some rows: results for them are incomplete (' + Object.keys(r.truncation.rows).join(', ') + ').') : null,
      r.limitations.length ? muted('Limitations: ' + r.limitations.join(' ')) : null,
      muted(r.note),
    ].filter(Boolean));
    const accounts = [['', 'all accounts']].concat(r.scopes.map(x => [x.account_key, x.account]));
    const currencies = [['', 'all currencies']].concat(Object.keys(s.amounts_by_status_minor.ledger).concat(Object.keys(s.amounts_by_status_minor.bank)).filter((c, i, a) => c !== 'unknown' && a.indexOf(c) === i).map(c => [c, c]));
    const f = ST.filters;
    const st = select('bsr-f-status', [['', 'all statuses']].concat(Object.entries(STATUS_LABEL)), f.status, 'Status');
    const ac = select('bsr-f-account', accounts, f.account, 'Account');
    const cu = select('bsr-f-currency', currencies, f.currency, 'Currency');
    const fr = el('input', { type: 'date', id: 'bsr-f-from', value: f.from });
    const to = el('input', { type: 'date', id: 'bsr-f-to', value: f.to });
    const q = el('input', { type: 'text', id: 'bsr-f-q', value: f.q, placeholder: 'transaction ID', class: 'col-filter-select' });
    setChildren($('bsr-filters'), el('div', { class: 'di-toolbar' }, [st, ac, cu, fr, to, q, button('Apply', () => {
      Object.assign(ST.filters, { status: st.value, account: ac.value, currency: cu.value, from: fr.value, to: to.value, q: q.value.trim() });
      ST.page = 1; loadResults();
    }, false, 'bsr-apply')]));
    $('bsr-results-card').hidden = false;
    loadResults();
    loadSide();
  }
  async function loadResults(){
    const r = ST.run, f = ST.filters;
    try {
      const p = await LS_API.get('/api/reconciliation/runs/' + r.run_id + '/results' + LS_API.qs({ status: f.status, account: f.account, currency: f.currency, from: f.from, to: f.to, q: f.q, page: ST.page, size: ST.size }));
      const rows = p.items.map(x => [badge(x.status), x.first_date, x.ledger_txn_ids.join(', ') || '—', x.bank_txn_ids.join(', ') || '—',
        minor((x.ledger_rows[0] || x.bank_rows[0] || {}).amount_minor, x.scope.currency), x.amount_difference_minor === null ? '—' : minor(x.amount_difference_minor, x.scope.currency),
        x.date_difference_days === null ? '—' : (typeof x.date_difference_days === 'object' ? x.date_difference_days.min + '…' + x.date_difference_days.max : x.date_difference_days),
        x.evidence_score === null ? '—' : lsScore(x.evidence_score)]);
      const pages = Math.max(1, Math.ceil(p.total / p.size));
      setChildren($('bsr-results'), [
        muted(lsInt(p.total) + ' results · page ' + p.page + ' of ' + pages + ' · evidence score ranks candidates and is not a probability'),
        table(['Status', 'Date', 'Ledger', 'Bank', 'Amount', 'Δ amount (bank − ledger)', 'Δ days', 'Evidence score'], rows, i => showDetail(p.items[i].result_id)),
        el('div', { class: 'di-toolbar' }, [button('Previous', () => { if(ST.page > 1){ ST.page--; loadResults(); } }), button('Next', () => { if(ST.page < pages){ ST.page++; loadResults(); } })]),
      ]);
    } catch(e){ setChildren($('bsr-results'), na('Results not available: ' + errText(e))); }
  }
  async function showDetail(id){
    try {
      const d = await LS_API.get('/api/reconciliation/runs/' + ST.run.run_id + '/results/' + id), x = d.result;
      const ev = d.source_evidence.map(e => [e.side, e.canonical.txn_id, e.original_filename + ' row ' + e.source_row, e.canonical.posted_at, minor(e.canonical.amount_minor, e.canonical.currency),
        e.canonical.direction, e.canonical.reference, e.canonical.description || e.canonical.payee_raw || e.canonical.payer_raw,
        Object.keys(e.p4_normalized_values).length ? Object.entries(e.p4_normalized_values).map(([k, v]) => k + '=' + JSON.stringify(v)).join('; ') : 'none']);
      setChildren($('bsr-detail'), [
        el('h4', null, [badge(x.status), ' ', x.result_id]),
        el('p', null, x.explanation),
        kv({ 'Account': x.scope.account, 'Currency': x.scope.currency, 'Direction': x.scope.flow, 'Amount difference (bank − ledger)': x.amount_difference_minor === null ? null : minor(x.amount_difference_minor, x.scope.currency),
          'Date difference (days)': x.date_difference_days === null ? null : JSON.stringify(x.date_difference_days), 'Evidence score (not a probability)': x.evidence_score === null ? null : lsScore(x.evidence_score),
          'Discrepancies': x.discrepancies.join(', ') || null, 'Reference / counterparty': x.link.reference || x.link.counterparty ? (x.link.reference || '—') + ' / ' + (x.link.counterparty || '—') : null }),
        el('h4', null, 'Source evidence (canonical values, P4 normalisations)'),
        table(['Side', 'ID', 'Evidence', 'Date', 'Amount', 'Direction', 'Reference', 'Description / party', 'P4 changes'], ev),
        x.components.length ? el('h4', null, 'Grouped components') : null,
        x.components.length ? table(['Side', 'ID', 'Date', 'Amount', 'Δ days'], x.components.map(c => [c.side, c.txn_id, c.date, minor(c.amount_minor, x.scope.currency), c.date_difference_days])) : null,
        x.candidates.length ? el('h4', null, 'Competing candidates (none selected)') : null,
        x.candidates.length ? table(['Ledger / settlement', 'Bank / components', 'Evidence score', 'Δ amount', 'Δ days'], x.candidates.map(c => c.type === 'pair'
          ? [c.ledger_txn_id, c.bank_txn_id, lsScore(c.evidence_score), minor(c.amount_difference_minor, x.scope.currency), c.date_difference_days]
          : [c.settlement, c.components.join(', '), '—', '—', '—'])) : null,
        d.findings.length ? muted('Findings: ' + d.findings.map(s => s.rule_id + ' (' + s.severity + ')').join(', ')) : null,
        (d.analyst_decisions || []).length ? muted('Analyst decisions (recorded; this computed result is unchanged): ' + d.analyst_decisions.map(a => a.action_text + ' — ' + a.rationale).join(' | ')) : null,
        window.LS_INV ? window.LS_INV.button({ link_type: 'reconciliation_result', run_id: ST.run.run_id, ref_id: x.result_id }, 'reconciliation result ' + x.result_id) : null,
        x.limitations.length ? muted('Limitations: ' + x.limitations.join(' ')) : null,
      ].filter(Boolean));
    } catch(e){ setChildren($('bsr-detail'), na('Detail not available: ' + errText(e))); }
  }
  async function loadSide(){
    const id = ST.run.run_id;
    try {
      const [dup, bal, fin] = await Promise.all([LS_API.get('/api/reconciliation/runs/' + id + '/duplicates' + LS_API.qs({ size: 100 })),
        LS_API.get('/api/reconciliation/runs/' + id + '/balances'), LS_API.get('/api/reconciliation/runs/' + id + '/findings' + LS_API.qs({ size: 100 }))]);
      const balRows = bal.accounts.map(a => [a.account, a.currency, a.status, a.reason || (a.order || '—'), minor(a.opening_balance_minor, a.currency), minor(a.movement_minor, a.currency),
        minor(a.closing_balance_minor, a.currency), a.discontinuities ? lsInt(a.discontinuities.length) : '—']);
      setChildren($('bsr-side'), [
        el('h4', null, 'Balance check'),
        bal.status === 'unavailable' && bal.reason ? na('Unavailable: ' + bal.reason) : table(['Account', 'Currency', 'Status', 'Order / reason', 'Opening (implied)', 'Movement', 'Closing', 'Breaks'], balRows),
        el('h4', null, 'Duplicate candidates'),
        muted(dup.note),
        table(['Side', 'Type', 'Rows', 'Amount', 'Dates', 'Member statuses'], dup.items.map(g => [g.side, g.type, g.txn_ids.join(', '), minor(g.amount_minor, g.currency), g.first_date + ' … ' + g.last_date,
          Object.values(g.member_status).join(', ')])),
        el('h4', null, 'Reconciliation findings (separate from P9 risk)'),
        table(['Rule', 'Severity', 'Ledger', 'Bank', 'Explanation'], fin.items.map(s => [s.rule_id, s.severity, s.ledger_txn_ids.join(', ') || '—', s.bank_txn_ids.join(', ') || '—', s.explanation])),
        fin.total > fin.items.length ? muted('Showing ' + fin.items.length + ' of ' + lsInt(fin.total) + ' findings.') : null,
      ].filter(Boolean));
    } catch(e){ setChildren($('bsr-side'), na('Not available: ' + errText(e))); }
  }

  async function ocrStatus(){
    try {
      const o = await LS_API.get('/api/pdf/ocr-status');
      setText('bsr-pdf-status', o.available ? 'Local OCR available: Tesseract ' + o.version + ' (' + o.languages.join('+') + '). Scanned pages are recognised on this machine; every OCR-derived row needs review.'
        : 'Local OCR UNAVAILABLE: ' + o.reason + '. Text PDFs still work; scanned pages are reported as not processed.');
    } catch(e){ setText('bsr-pdf-status', 'OCR status not available: ' + e.message); }
  }

  ocrStatus();
  loadDatasets();
  document.addEventListener('ls:dataset', ev => { ST.ledger = ev.detail.datasetId; ST.bank = ''; loadDatasets(); });
})();

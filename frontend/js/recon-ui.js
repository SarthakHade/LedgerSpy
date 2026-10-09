/* ============================================================
   LedgerSpy — P14 bank statement reconciliation view
   ============================================================
   Boundary: statuses, counts, amounts, differences, evidence scores,
   duplicates, balance checks and findings come from /api/reconciliation…
   only. The page computes nothing: it lists datasets, sends the analyst's
   confirmations (bank mapping semantics, scopes, ledger debit meaning) and
   renders the backend's results. Amounts are integer minor units turned
   into exact decimal text. All data-derived text is set with textContent.
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
  const ST = { datasets: [], ledger: '', bank: '', setup: null, run: null, filters: { status: '', account: '', currency: '', from: '', to: '', q: '' }, page: 1, size: 50 };

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
    try { ST.datasets = (await LS_API.get('/api/datasets')).items.filter(d => d.status === 'canonicalised'); }
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
    const file = el('input', { type: 'file', id: 'bsr-file', accept: '.csv,.xlsx', style: 'display:none' });
    file.addEventListener('change', () => { if(file.files[0]) importStatement(file.files[0]); file.value = ''; });
    setChildren($('bsr-pickers'), [
      el('div', { class: 'di-toolbar' }, [muted('Ledger'), ls, muted('Bank statement'), bs,
        button('Import bank statement (CSV / XLSX)', () => { if(!ST.ledger){ say('Choose the ledger first: the statement is imported into its case.'); return; } file.click(); }, false, 'bsr-import'), file]),
    ]);
    if(ST.ledger && ST.bank) loadSetup(); else setChildren($('bsr-setup'), na(ST.ledger ? 'Import or choose a bank statement of the same case to reconcile.' : 'Import a ledger first (top of the page).'));
  }
  function say(t){ setText('bsr-msg', t); }

  /* ---------------- import: upload → confirm bank semantics → integrity ---------------- */
  async function importStatement(f){
    const caseId = (ST.datasets.find(d => d.dataset_id === ST.ledger) || {}).case_id;
    say('Uploading ' + f.name + ' as a bank statement…');
    try { showMapping(await LS_API.upload(caseId, f, 'bank_statement')); }
    catch(e){ say('Import refused: ' + errText(e)); }
  }
  function showMapping(ds){
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
    });
    Object.values(inputs).forEach(n => parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:6px' }, [n])));
    const msg = el('div', { class: 'ls-muted', id: 'bsr-map-msg', style: 'margin-top:8px' });
    parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:12px' }, [
      button('Confirm statement mapping', async () => {
        if(inputs.debit){ if(!inputs.debit.value){ msg.textContent = 'Confirm what a debit means first.'; return; } proposed.bank.debit_means = inputs.debit.value; }
        if(inputs.sign){ if(!inputs.sign.value){ msg.textContent = 'Confirm the amount sign first.'; return; } proposed.bank.amount_sign = inputs.sign.value; }
        if(inputs.currency && inputs.currency.value.trim()) proposed.options.currency = inputs.currency.value.trim().toUpperCase();
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
    setChildren($('bsr-setup'), na('Loading what must be confirmed…'));
    try { ST.setup = await LS_API.get('/api/reconciliation/setup' + LS_API.qs({ ledger_dataset_id: ST.ledger, bank_dataset_id: ST.bank })); }
    catch(e){ setChildren($('bsr-setup'), na('Setup not available: ' + errText(e))); return; }
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

  loadDatasets();
  document.addEventListener('ls:dataset', ev => { ST.ledger = ev.detail.datasetId; ST.bank = ''; loadDatasets(); });
})();

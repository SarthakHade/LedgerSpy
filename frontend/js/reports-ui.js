/* ============================================================
   LedgerSpy — P16 audit reports (Reports tab)
   ============================================================
   Boundary: report content, counts, totals, findings and evidence come
   from /api/reports… only (plus the existing case / investigation / run
   lists to choose the subject). Nothing is computed here. Automated
   indicators, analyst assessments and conclusions are shown apart; stale,
   changed or unconfirmed sources are flagged as the API reports them.
   All text is set with textContent (el()). Downloads use the session token
   through LS_API.blob; no token ever appears in a URL.
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('rep-app')) return;

  const $ = id => document.getElementById(id);
  const ST = { cases: [], caseId: '', vocab: null, type: 'case_investigation', invs: [], runs: [], selected: new Set(), runId: '' };

  function muted(t){ return el('div', { class: 'ls-muted' }, t); }
  function na(t){ return el('div', { class: 'na-inline' }, t); }
  function warn(t){ return el('div', { class: 'na-inline', style: 'border-left:3px solid var(--amber);padding-left:8px' }, t); }
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
  function table(headers, rows, empty){
    return el('div', { class: 'tx-table-wrap di-table' }, el('table', { class: 'tx-table' }, [
      el('thead', null, el('tr', null, headers.map(h => el('th', null, h)))),
      el('tbody', null, rows.length ? rows.map(r => el('tr', null, r.map(c => el('td', null, c instanceof Node ? c : (c === null || c === undefined ? '—' : String(c)))))) :
        el('tr', null, el('td', { colspan: String(headers.length) }, empty || 'None.'))),
    ]));
  }
  function h(t){ return el('div', { class: 'detail-h' }, t); }
  function money(m){ return m ? m.text : 'unavailable'; }
  function errText(e){ return e.message + (e.details && Object.keys(e.details).length ? ' ' + JSON.stringify(e.details) : ''); }
  function say(t){ setText('rep-msg', t); }
  function refsText(refs){
    return (refs || []).map(r => r.link_type + ' ' + r.ref_id + ' [' + r.verification + ']' +
      (r.evidence.length ? ' · evidence ' + r.evidence.map(e => e.sha256.slice(0, 12) + '… ' + e.verification).join(', ') : '')).join(' | ') || 'none';
  }

  if(!LS_API.available){ setChildren($('rep-controls'), na('Not available: reports are built by the local LedgerSpy service (`ledgerspy serve`).')); return; }

  /* ---------------- subject selection ---------------- */
  async function load(){
    try {
      ST.vocab = ST.vocab || await LS_API.get('/api/reports/vocabulary');
      const ds = (await LS_API.get('/api/datasets')).items;
      const seen = {};
      ST.cases = ds.filter(d => !seen[d.case_id] && (seen[d.case_id] = true)).map(d => ({ case_id: d.case_id, name: d.case_name }));
      ST.datasets = ds;
    } catch(e){ setChildren($('rep-controls'), na('Could not load: ' + errText(e))); return; }
    if(!ST.caseId && ST.cases.length) ST.caseId = ST.cases[0].case_id;
    await loadSubjects();
  }
  async function loadSubjects(){
    if(!ST.caseId){ setChildren($('rep-controls'), na('No engagement case yet: import a ledger first.')); return; }
    try {
      ST.invs = (await LS_API.get('/api/investigations' + LS_API.qs({ case_id: ST.caseId, size: 200 }))).items;
      const mine = new Set(ST.datasets.filter(d => d.case_id === ST.caseId).map(d => d.dataset_id));
      ST.runs = (await LS_API.get('/api/reconciliation/runs')).items.filter(r => mine.has(r.ledger_dataset_id));
    } catch(e){ setChildren($('rep-controls'), na('Could not load the case: ' + errText(e))); return; }
    ST.selected = new Set([...ST.selected].filter(id => ST.invs.some(i => i.investigation_id === id)));
    renderControls();
    loadArtifacts();
  }
  function renderControls(){
    const cs = select('rep-case', ST.cases.map(c => [c.case_id, c.name]), ST.caseId, 'Engagement case');
    const ty = select('rep-type', Object.entries(ST.vocab.report_types), ST.type, 'Report type');
    cs.addEventListener('change', () => { ST.caseId = cs.value; ST.selected.clear(); loadSubjects(); });
    ty.addEventListener('change', () => { ST.type = ty.value; renderControls(); });
    const parts = [el('div', { class: 'di-toolbar' }, [muted('Engagement case'), cs, muted('Report'), ty])];
    if(ST.type === 'bank_reconciliation'){
      const rs = select('rep-run', [['', 'choose a reconciliation run']].concat(ST.runs.map(r => [r.run_id, r.run_id + ' · ' + r.created_at + (r.stale ? ' · STALE (inputs changed)' : '')])), ST.runId, 'Run');
      rs.addEventListener('change', () => { ST.runId = rs.value; });
      parts.push(ST.runs.length ? rs : na('No reconciliation run in this engagement case.'));
    } else {
      const single = ST.type === 'case_investigation';
      parts.push(muted(single ? 'Choose one investigation:' : 'Choose the investigations to include (same engagement case):'));
      parts.push(ST.invs.length ? el('div', { id: 'rep-invs' }, ST.invs.map(i => {
        const cb = el('input', { type: single ? 'radio' : 'checkbox', name: 'rep-inv', value: i.investigation_id, id: 'rep-inv-' + i.investigation_id });
        cb.checked = ST.selected.has(i.investigation_id);
        cb.addEventListener('change', () => {
          if(single) ST.selected = new Set([i.investigation_id]);
          else if(cb.checked) ST.selected.add(i.investigation_id); else ST.selected.delete(i.investigation_id);
        });
        return el('label', { style: 'display:block' }, [cb, ' ' + i.title + ' — ' + i.status.replace(/_/g, ' ') + (i.outcome ? ' (' + i.outcome.replace(/_/g, ' ') + ')' : '')]);
      })) : na('No investigation in this engagement case yet (Investigations tab).'));
    }
    parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:8px' }, [
      button('Preview', preview, false, 'rep-preview'), button('Generate and store', generate, true, 'rep-generate')]));
    if(ST.type === 'audit_memo') parts.push(warn(ST.vocab.draft_notice));
    parts.push(muted('Formats: ' + ST.vocab.formats.join(', ') + '. Not built: ' + Object.entries(ST.vocab.deferred_formats).map(([k, v]) => k + ' (' + v + ')').join('; ') + '.'));
    setChildren($('rep-controls'), parts);
  }
  function body(){
    if(ST.type === 'bank_reconciliation') return { report_type: ST.type, run_id: ST.runId || null };
    return { report_type: ST.type, investigation_ids: [...ST.selected] };
  }

  /* ---------------- preview ---------------- */
  async function preview(){
    say('Building preview…');
    try {
      const r = await LS_API.post('/api/reports/preview', body());
      say('Preview built from the current records (not stored). Content SHA-256 ' + r.content.content_sha256 + '.');
      renderContent(r.content, null);
    } catch(e){ say('Preview refused: ' + errText(e)); }
  }
  async function generate(){
    say('Generating…');
    try {
      const r = await LS_API.post('/api/reports', body());
      say('Stored ' + r.report_type.replace(/_/g, ' ') + ' version ' + r.version + ' as ' + r.report_id + (r.status === 'draft' ? ' (draft)' : '') + '.');
      const full = await LS_API.get('/api/reports/' + r.report_id);
      renderContent(full.content, full.envelope);
      loadArtifacts();
    } catch(e){ say('Generation refused: ' + errText(e)); }
  }

  function claimRows(claims, status){
    return claims.map(c => [c.origin, c.text].concat(status ? [(c.analyst_status || '—').replace(/_/g, ' ')] : []).concat([c.traceability, refsText(c.references), (c.limitations || []).join(' ') || '—']));
  }
  function warnings(c){
    const out = [];
    const q = c.data_quality || {};
    Object.entries(q).forEach(([k, v]) => out.push(warn(v.length + ' reference(s) ' + k.replace(/_/g, ' ') + ': ' + v.join(', '))));
    (c.scope && c.scope.reconciliation_runs || []).filter(r => r.stale).forEach(r => out.push(warn('Reconciliation run ' + r.run_id + ' is STALE: its results are not current.')));
    (c.scope && c.scope.datasets || []).filter(d => d.evidence_verification !== 'verified').forEach(d => out.push(warn('Evidence of ' + d.original_filename + ': ' + d.evidence_verification)));
    return out;
  }
  function caseView(c){
    return [
      h('Automated findings (indicators — not proof)'),
      table(['Origin', 'Statement', 'Analyst status', 'Traceability', 'References and evidence', 'Limitations'], claimRows(c.automated_findings, true)),
      h('Linked records'),
      table(['Origin', 'Statement', 'Traceability', 'References and evidence', 'Limitations'], claimRows(c.linked_records, false)),
      h('Analyst assessments (recorded decisions)'),
      table(['Subject', 'Action', 'Rationale', 'Contradicting evidence', 'Open', 'By / at'], c.analyst_assessments.map(a => [a.subject, a.action_text, a.text, refsText(a.contradicting) === 'none' ? 'none recorded' : refsText(a.contradicting), a.unresolved ? 'yes' : 'no', a.created_by + ' ' + a.created_at]), 'No analyst assessment recorded.'),
      h('Analyst notes'),
      table(['Subject', 'Note (verbatim)', 'By / at'], c.analyst_notes.map(n => [n.subject, n.text, n.created_by + ' ' + n.created_at]), 'No notes.'),
      h('Conclusion'),
      c.conclusion ? el('p', null, 'Analyst-recorded outcome ' + c.conclusion.outcome.replace(/_/g, ' ') + ': ' + c.conclusion.text) : na('No conclusion recorded. The report draws none.'),
      h('Values of linked transactions'),
      muted(c.linked_values.note),
      table(['Dataset', 'Currency', 'Transactions', 'Gross value'], c.linked_values.totals.map(t => [t.dataset_id, t.currency, lsInt(t.transactions), money(t.gross_value)]), 'No transaction is linked directly.'),
    ];
  }
  function reconView(c){
    const out = [h('Run ' + c.run.run_id)];
    if(c.run_state !== 'current'){
      out.push(warn('Run state ' + c.run_state.toUpperCase() + ': ' + c.run_state_reason));
      return out.concat([muted('Recommendations: ' + c.recommendations.join(' '))]);
    }
    const sc = c.status_counts;
    out.push(table(['Status', 'Results', 'Ledger rows', 'Bank rows'], Object.entries(sc.results).map(([s, n]) => [s.replace(/_/g, ' '), lsInt(n), lsInt(sc.rows.ledger[s]), lsInt(sc.rows.bank[s])])));
    out.push(h('Totals by account, currency and status'), muted(c.totals.note), muted(c.totals.check));
    out.push(table(['Side', 'Account', 'Currency', 'Status', 'Rows', 'Value'], c.totals.breakdown.map(b => [b.side, b.account, b.currency, b.status.replace(/_/g, ' '), lsInt(b.rows), money(b.value)])));
    out.push(h('Exceptions (' + lsInt(c.exceptions.total) + (c.exceptions.truncated ? ', first ' + lsInt(c.exceptions.listed) + ' listed' : '') + ')'));
    out.push(table(['Result', 'Status', 'Account', 'Ledger', 'Bank', 'Difference'], c.exceptions.items.slice(0, 50).map(r => [r.result_id, r.status.replace(/_/g, ' '), r.account,
      r.ledger_rows.map(x => x.txn_id + ' ' + money(x.amount)).join('; ') || '—', r.bank_rows.map(x => x.txn_id + ' ' + money(x.amount)).join('; ') || '—', r.amount_difference ? money(r.amount_difference) : '—'])));
    out.push(muted('Balance check: ' + c.balance_check.status + (c.balance_check.reason ? ' — ' + c.balance_check.reason : '') + '. Extracted lines: ' + lsInt(c.extraction.bank_rows_read_by_ocr) + ' read by OCR; ' + lsInt(c.extraction.results_with_unconfirmed_extraction) + ' result(s) with unconfirmed extraction.'));
    return out;
  }
  function memoView(c){
    const out = [warn(c.approval.note)];
    c.sections.forEach(s => {
      out.push(h(s.title));
      (s.paragraphs || []).forEach(p => out.push(el('p', null, p)));
      if((s.items || []).length) out.push(el('ul', { class: 'ls-list' }, s.items.map(i => el('li', null, i))));
      if((s.findings || []).length) out.push(table(['Statement', 'Origin', 'Evidence'], s.findings.map(f => [f.text, f.origin, f.evidence.map(x => x.slice(0, 12) + '…').join(', ') || 'none'])));
    });
    out.push(muted('Numbers check: ' + c.numbers_check.rule + ' (' + lsInt(c.numbers_check.numbers_stated) + ' numbers).'));
    return out;
  }
  function manifestView(c){
    return [
      muted(c.note),
      h('Evidence files'),
      table(['SHA-256', 'File names', 'Bytes', 'Verification', 'Referenced by'], c.evidence_files.map(f => [f.sha256, f.original_filenames.join(', '), lsInt(f.size_bytes), f.verification, lsInt(f.referenced_by.length) + ' reference(s)'])),
      h('References'),
      table(['Link', 'Type', 'Reference', 'Active', 'Verification', 'Location'], c.references.map(e => [e.link_id, e.link_type, e.ref_id, e.active ? 'yes' : 'no (removed)', e.verification,
        e.locations.map(x => [x.side, x.filename, x.source_row !== undefined ? 'row ' + x.source_row : null, x.pdf_page !== undefined ? 'page ' + x.pdf_page : null].filter(Boolean).join(' ')).join('; ') || '—'])),
    ];
  }
  function renderContent(c, env){
    const head = [el('h3', null, c.title),
      muted((env ? 'Report ' + env.report_id + ' · version ' + env.version + ' · ' + env.status + ' · generated ' + env.generated_at + ' by ' + env.generated_by + ' · ' : 'PREVIEW (not stored) · ') + 'content SHA-256 ' + c.content_sha256 + ' · schema ' + c.schema_version)];
    const view = { case_investigation: caseView, bank_reconciliation: reconView, audit_memo: memoView, evidence_manifest: manifestView }[c.report_type](c);
    setChildren($('rep-preview-body'), head.concat(warnings(c), view));
  }

  /* ---------------- stored artefacts ---------------- */
  async function loadArtifacts(){
    try {
      const p = await LS_API.get('/api/reports' + LS_API.qs({ case_id: ST.caseId, size: 50 }));
      setChildren($('rep-list'), [muted(lsInt(p.total) + ' stored report(s) in this engagement case (earlier versions are kept)'),
        table(['Report', 'Type', 'Version', 'Status', 'Generated', 'Content SHA-256', 'Open / download'], p.items.map(r => [r.report_id, r.report_type.replace(/_/g, ' '), r.version, r.status, r.generated_at + ' · ' + r.generated_by, r.content_sha256.slice(0, 16) + '…',
          el('span', null, [button('Open', () => openReport(r.report_id)), button('Verify', () => verify(r.report_id))].concat(ST.vocab.formats.map(f => button(f.toUpperCase(), () => download(r, f)))))]), 'No report stored yet.')]);
    } catch(e){ setChildren($('rep-list'), na('Could not list reports: ' + errText(e))); }
  }
  async function openReport(id){
    try { const r = await LS_API.get('/api/reports/' + id); renderContent(r.content, r.envelope); say('Showing stored report ' + id + '.'); }
    catch(e){ say('Could not open the report: ' + errText(e)); }
  }
  async function verify(id){
    try { const v = await LS_API.get('/api/reports/' + id + '/verify'); say('Report ' + id + ': ' + Object.entries(v.artifacts).map(([f, s]) => f + ' ' + s).join(', ') + '.'); }
    catch(e){ say('Could not verify: ' + errText(e)); }
  }
  async function download(r, fmt){
    try {
      const blob = await LS_API.blob('/api/reports/' + r.report_id + '/download' + LS_API.qs({ format: fmt }));
      const a = el('a', { href: URL.createObjectURL(blob), download: 'ledgerspy-' + r.report_type + '-v' + r.version + '-' + r.report_id + '.' + fmt });
      document.body.appendChild(a); a.click(); a.remove();
      say('Downloaded ' + fmt.toUpperCase() + ' of ' + r.report_id + '.');
    } catch(e){ say('Download refused: ' + errText(e)); }
  }

  window.LS_REPORTS = { reload: load };
  load();
})();

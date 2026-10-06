/* ============================================================
   LedgerSpy P4 — backend Data Integrity report (Data Integrity tab).

   Boundary: every number in this panel comes from the ledgerspy API
   (P4 integrity report, issues, fixes, transformation log). Nothing is
   computed or estimated here. It works only when the page is served by
   `ledgerspy serve` (session token in <meta name="ledgerspy-session-token">);
   opened as a plain file it says so. Datasets reach the backend through
   the API (POST /api/cases/{id}/files). When the risk UI (js/risk-ui.js) analyses a
   dataset it announces it ('ls:dataset') and this panel follows.
   All text is rendered with textContent via el() (no innerHTML).
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined') return;
  const root = document.getElementById('di-backend-body');
  if(!root) return;
  const meta = document.querySelector('meta[name="ledgerspy-session-token"]');
  const SEV = ['info', 'warning', 'error', 'critical'];
  const SEV_COLOR = { info:'var(--txt3)', warning:'var(--amber)', error:'var(--red)', critical:'var(--red)' };
  const state = { datasetId: null, report: null, issuePage: 1, issueCheck: '', message: '' };

  function clear(node){ while(node.firstChild) node.removeChild(node.firstChild); }
  function show(...nodes){ clear(root); nodes.flat().forEach(n => n && root.appendChild(n)); }
  function na(text){ return el('div', { class:'na-card' }, text); }
  function fmtPct(x){ return (Math.round(x * 10000) / 100).toString() + '%'; }
  function cell(v){ return v === null || v === undefined ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v)); }
  function table(headers, rows){
    return el('div', { class:'tx-table-wrap di-table' }, el('table', { class:'tx-table' }, [
      el('thead', null, el('tr', null, headers.map(h => el('th', null, h)))),
      el('tbody', null, rows.length ? rows.map(r => el('tr', null, r.map(c => el('td', null, c instanceof Node ? c : cell(c)))))
                                    : el('tr', null, el('td', { colspan: String(headers.length) }, 'None.')))
    ]));
  }
  function tile(label, value, color){
    return el('div', { class:'di-tile' }, [ el('div', { class:'di-tile-v', style: color ? 'color:' + color : null }, cell(value)),
                                            el('div', { class:'di-tile-l' }, label) ]);
  }
  function section(title, children){ return el('div', { class:'di-section' }, [ el('h4', null, title), children ]); }
  function button(label, onClick, primary){
    const b = el('button', { class: primary ? 'btn btn-primary di-btn' : 'btn btn-secondary di-btn', type:'button' }, label);
    b.addEventListener('click', onClick);
    return b;
  }

  if(!meta){
    show(na('Not available here: the backend integrity report (P4) needs LedgerSpy to be opened through `ledgerspy serve`. ' +
            'Nothing is computed in the browser instead.'));
    return;
  }
  const token = meta.getAttribute('content');

  async function api(method, path, body){
    const init = { method, headers: { 'Authorization': 'Bearer ' + token }, credentials: 'same-origin' };
    if(body !== undefined){ init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    const r = await fetch(path, init);
    let data = {};
    try { data = await r.json(); } catch(e) { data = {}; }
    if(!r.ok){
      const err = new Error((data.error && data.error.message) || data.detail || ('HTTP ' + r.status));
      err.status = r.status; err.code = data.error && data.error.code; err.details = data.error && data.error.details;
      throw err;
    }
    return data;
  }

  /* ---------- dataset picker ---------- */
  async function loadDatasets(){
    let items;
    try { items = (await api('GET', '/api/datasets')).items; }
    catch(e){ show(na('Could not reach the backend: ' + e.message)); return; }
    const usable = items.filter(d => d.status === 'canonicalised');
    if(!usable.length){
      show(na('No canonical dataset in this workspace yet. Import a ledger through the API (POST /api/cases/{case}/files, ' +
              'then PUT /api/datasets/{id}/mapping); this panel then shows its integrity report.'));
      return;
    }
    const select = el('select', { class:'col-filter-select', 'aria-label':'Dataset' },
      usable.map(d => el('option', { value: d.dataset_id }, d.original_filename + ' · ' + d.case_name + ' · ' + d.row_count + ' rows')));
    select.addEventListener('change', () => { state.datasetId = select.value; state.issuePage = 1; state.issueCheck = ''; loadReport(); });
    state.datasetId = state.datasetId || usable[0].dataset_id;
    select.value = state.datasetId;
    state.select = select;
    state.picker = el('div', { class:'di-toolbar' }, [ el('span', { class:'di-muted' }, 'Dataset'), select,
      button('Run integrity check', runCheck, false) ]);
    loadReport();
  }

  async function runCheck(){
    try { state.message = ''; await api('POST', '/api/datasets/' + state.datasetId + '/integrity'); }
    catch(e){ state.message = 'Integrity check failed: ' + e.message; }
    loadReport();
  }

  async function loadReport(){
    let report;
    try { report = await api('GET', '/api/datasets/' + state.datasetId + '/integrity'); }
    catch(e){
      if(e.code === 'not_run'){ show(state.picker, na('No integrity check has been run on this dataset yet. Use "Run integrity check".')); return; }
      show(state.picker, na('Could not load the report: ' + e.message)); return;
    }
    state.report = report;
    const [issues, fixes, log] = await Promise.all([
      api('GET', '/api/datasets/' + state.datasetId + '/integrity/issues?size=50&page=' + state.issuePage +
                 (state.issueCheck ? '&check=' + encodeURIComponent(state.issueCheck) : '')),
      api('GET', '/api/datasets/' + state.datasetId + '/fixes'),
      api('GET', '/api/datasets/' + state.datasetId + '/transformations'),
    ]);
    render(report, issues, fixes.items, log.items);
  }

  /* ---------- report ---------- */
  function render(r, issues, fixes, log){
    const s = r.summary, ready = r.readiness;
    const statusText = { ready:'Ready', ready_with_warnings:'Ready with warnings', not_ready:'Not ready: blocking issues remain' }[ready.status];
    const head = el('div', { class:'di-readiness' }, [
      el('div', { class:'di-score', id:'di-backend-score' }, ready.score.toFixed(1) + '%'),
      el('div', null, [
        el('div', { class:'di-status' }, statusText + (ready.capped_by_blocking_issues ? ' (capped at ' + ready.score + ')' : '')),
        el('div', { class:'di-muted' }, 'Run ' + r.run_id + ' · ' + r.created_at + ' · report sha256 ' + r.report_sha256.slice(0, 16) + '…'),
        el('div', { class:'di-muted' }, ready.formula),
      ]),
    ]);
    const tiles = el('div', { class:'di-tiles' }, [
      tile('Total rows', s.total_rows), tile('Clean rows', s.clean_rows, 'var(--green)'),
      tile('Rows with issues', s.rows_with_issues, s.rows_with_issues ? 'var(--amber)' : null),
      tile('Rows with INFO only', s.rows_with_info_only),
      ...SEV.map(lvl => tile(lvl.toUpperCase() + ' issues', s.issues_by_severity[lvl], s.issues_by_severity[lvl] ? SEV_COLOR[lvl] : null)),
    ]);
    const components = table(['Component', 'Weight', 'Assessed', 'Rows affected', 'Clean share', 'Points', 'Points lost'],
      ready.components.map(c => [c.component, c.weight, c.assessed ? 'yes' : 'no (no check could run)', c.affected_rows,
                                 fmtPct(c.clean_share), c.points === null ? '—' : c.points.toFixed(2), c.points_lost === null ? '—' : c.points_lost.toFixed(2)]));
    const missing = table(['Field', 'Kind', 'Missing', '%'], [
      ...Object.entries(r.missing_values.required).map(([f, v]) => [f, 'required', v.missing, v.percent + '%']),
      ...Object.entries(r.missing_values.optional || {}).map(([f, v]) => [f, 'optional', v.missing, v.percent + '%']),
    ]);
    const dupFmt = el('div', { class:'di-tiles' }, [
      tile('Exact duplicate rows (redundant)', r.duplicates.exact.redundant_rows === undefined ? '—' : r.duplicates.exact.redundant_rows),
      tile('Near duplicates', r.duplicates.near), tile('Duplicate source IDs', r.duplicates.source_ids),
      tile('Invalid values', r.format_issues.invalid_value), tile('Unparseable dates', r.format_issues.unparseable_date),
      tile('Implausible dates', r.format_issues.implausible_date), tile('Malformed identifiers', r.format_issues.malformed_identifier),
    ]);
    const checks = table(['Check', 'Status', 'Issues', 'Rows', 'Severity', 'Basis / reason'],
      r.checks.map(c => [c.check, c.status === 'not_available' ? 'NOT AVAILABLE' : c.status, c.issue_count, c.affected_rows,
                         c.severity || '—', c.basis]));
    const coverage = table(['Canonical field', 'Coverage'], Object.entries(r.field_coverage).map(([f, v]) =>
      [f, v.status + (v.column ? ' (' + v.column + ')' : '') + (v.reason ? ': ' + v.reason : '')]));
    const limits = r.limitations.length ? el('ul', { class:'di-list' }, r.limitations.map(l => el('li', null, l))) : el('div', { class:'di-muted' }, 'None.');
    const unmapped = r.unmapped_columns.length ? el('div', { class:'di-muted' }, r.unmapped_columns.join(' · ')) : el('div', { class:'di-muted' }, 'None.');

    show(
      state.picker,
      state.message ? na(state.message) : null,
      head, tiles,
      section('Readiness components', components),
      section('Missing values', missing),
      section('Duplicates and format/type issues', dupFmt),
      section('Checks (available and not available)', checks),
      section('Unmapped source columns (kept verbatim, not validated)', unmapped),
      section('Field coverage', coverage),
      section('Limitations', limits),
      section('Issues (each linked to its transaction and source row)', renderIssues(r, issues)),
      section('Proposed fixes (nothing changes until accepted and applied)', renderFixes(fixes)),
      section('Transformation log', renderLog(log)),
    );
  }

  function renderIssues(r, issues){
    const filter = el('select', { class:'col-filter-select', 'aria-label':'Filter issues by check' },
      [el('option', { value:'' }, 'All checks'), ...r.checks.filter(c => c.issue_count).map(c => el('option', { value:c.check }, c.check + ' (' + c.issue_count + ')'))]);
    filter.value = state.issueCheck;
    filter.addEventListener('change', () => { state.issueCheck = filter.value; state.issuePage = 1; loadReport(); });
    const pages = Math.max(1, Math.ceil(issues.total / issues.size));
    const nav = el('div', { class:'di-toolbar' }, [ filter,
      el('span', { class:'di-muted' }, issues.total + ' issues · page ' + issues.page + ' of ' + pages),
      issues.page > 1 ? button('◀ Prev', () => { state.issuePage--; loadReport(); }) : null,
      issues.page < pages ? button('Next ▶', () => { state.issuePage++; loadReport(); }) : null ]);
    const rows = issues.items.map(i => [i.source_row === null ? 'dataset' : 'row ' + i.source_row, i.txn_id || '—',
      el('span', { style:'color:' + SEV_COLOR[i.severity] }, i.severity.toUpperCase()), i.check, i.field || '—', i.message,
      i.fix_id ? i.fix_id + ' (' + (i.fix_status || '—') + ')' : '—']);
    return el('div', null, [nav, table(['Where', 'Transaction', 'Severity', 'Check', 'Field', 'Message', 'Fix'], rows)]);
  }

  async function decide(decisions){
    try { state.message = ''; await api('POST', '/api/datasets/' + state.datasetId + '/fixes/decisions', { decisions }); }
    catch(e){ state.message = 'Decision not recorded: ' + e.message; }
    loadReport();
  }

  function renderFixes(fixes){
    const open = fixes.filter(f => ['proposed', 'accepted', 'rejected', 'reverted'].includes(f.status));
    // Bulk accept only touches undecided fixes: it must never override an analyst's rejection.
    const safe = open.filter(f => !f.requires_confirmation && f.status === 'proposed');
    const accepted = fixes.filter(f => f.status === 'accepted').length;
    const actions = el('div', { class:'di-toolbar' }, [
      safe.length ? button('Accept all ' + safe.length + ' safe fixes', () => decide(safe.map(f => ({ fix_id: f.fix_id, decision: 'accept' })))) : null,
      button('✨ Auto-Fix: apply ' + accepted + ' accepted fix' + (accepted === 1 ? '' : 'es'), async () => {
        try { state.message = ''; const out = await api('POST', '/api/datasets/' + state.datasetId + '/normalize');
              state.message = 'Applied ' + out.applied.length + ' transformation(s)' + (out.conflicts.length ? ', ' + out.conflicts.length + ' conflict(s)' : '') + '; integrity re-run.'; }
        catch(e){ state.message = 'Normalisation failed: ' + e.message; }
        loadReport();
      }, true),
    ]);
    const rows = fixes.map(f => {
      const controls = [];
      if(['proposed', 'rejected', 'reverted', 'accepted'].includes(f.status)){
        let choice = null;
        if(f.candidates.length > 1 && f.status !== 'accepted'){
          choice = el('select', { class:'col-filter-select', 'aria-label':'Choose value' },
            [el('option', { value:'' }, 'choose…'), ...f.candidates.map((c, i) => el('option', { value:String(i) }, cell(c)))]);
          controls.push(choice);
        }
        if(f.status !== 'accepted') controls.push(button('Accept', () => {
          if(choice && choice.value === ''){ state.message = 'Choose one of the candidate values first (' + f.fix_id + ').'; loadReport(); return; }
          decide([{ fix_id: f.fix_id, decision: 'accept', value: choice ? f.candidates[Number(choice.value)] : undefined }]);
        }));
        if(f.status !== 'rejected'){
          const reason = el('input', { class:'col-filter-select di-reason', type:'text', placeholder:'reason to reject', 'aria-label':'Rejection reason' });
          controls.push(reason, button('Reject', () => decide([{ fix_id: f.fix_id, decision: 'reject', reason: reason.value }])));
        }
      }
      return [f.source_row, f.field, f.transform, cell(f.original_value), f.candidates.length > 1 ? f.candidates.map(cell).join(' | ') : cell(f.proposed_value),
              f.requires_confirmation ? 'yes' : 'no', f.status + (f.decision_reason ? ' (' + f.decision_reason + ')' : ''), el('div', { class:'di-controls' }, controls)];
    });
    return el('div', null, [actions, table(['Row', 'Field', 'Transform', 'Original', 'Proposed', 'Needs confirmation', 'Status', 'Decision'], rows)]);
  }

  function renderLog(log){
    const reverted = new Set(log.filter(t => t.kind === 'revert').map(t => t.reverts));
    const rows = log.map(t => {
      let action = '—';
      if(t.kind === 'apply' && !reverted.has(t.transformation_id)){
        const reason = el('input', { class:'col-filter-select di-reason', type:'text', placeholder:'reason to revert', 'aria-label':'Revert reason' });
        action = el('div', { class:'di-controls' }, [reason, button('Revert', async () => {
          try { state.message = ''; await api('POST', '/api/datasets/' + state.datasetId + '/transformations/' + t.transformation_id + '/revert', { reason: reason.value });
                await api('POST', '/api/datasets/' + state.datasetId + '/integrity'); }
          catch(e){ state.message = 'Revert failed: ' + e.message; }
          loadReport();
        })]);
      }
      return [t.applied_at, t.kind, 'row ' + t.source_row, t.field, t.transform, cell(t.original_value), cell(t.normalized_value), t.applied_by, t.reason, action];
    });
    return table(['When', 'Kind', 'Row', 'Field', 'Transform', 'Original', 'Normalised', 'By', 'Reason', ''], rows);
  }

  document.addEventListener('ls:dataset', e => {
    state.datasetId = e.detail.datasetId; state.issuePage = 1; state.issueCheck = '';
    loadDatasets();  // refresh the picker (a new import may have arrived) and show this dataset's report
  });

  loadDatasets();
})();

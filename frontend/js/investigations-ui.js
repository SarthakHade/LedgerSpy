/* ============================================================
   LedgerSpy — P15 investigation workflow (Investigations tab)
   ============================================================
   Boundary: cases, links, decisions, notes, activity, summaries and
   manifests come from /api/investigations… only; the page sends the
   analyst's explicit actions and renders the backend's records. Automated
   findings are shown as indicators that require review; analyst decisions
   are shown separately and never as a change of a score. All user text
   (titles, notes, rationales) is set with textContent (el()).
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('iv-app')) return;

  const $ = id => document.getElementById(id);
  const ST = { cases: [], caseId: '', status: '', vocab: null, current: null, pending: null };
  const STATUS_CLASS = { open: 'bg-neutral', in_review: 'bg-amber', awaiting_evidence: 'bg-amber', concluded: 'bg-green', closed: 'bg-neutral' };

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
  function input(id, placeholder, value){ return el('input', { type: 'text', class: 'col-filter-select', id: id, placeholder: placeholder, value: value || '' }); }
  function area(id, placeholder){ return el('textarea', { class: 'col-filter-select', id: id, placeholder: placeholder, rows: '3', style: 'width:100%' }); }
  function table(headers, rows){
    return el('div', { class: 'tx-table-wrap di-table' }, el('table', { class: 'tx-table' }, [
      el('thead', null, el('tr', null, headers.map(h => el('th', null, h)))),
      el('tbody', null, rows.length ? rows.map(r => el('tr', null, r.map(c => el('td', null, c instanceof Node ? c : (c === null || c === undefined ? '—' : String(c)))))) :
        el('tr', null, el('td', { colspan: String(headers.length) }, 'None.'))),
    ]));
  }
  function badge(s){ return el('span', { class: 'badge ' + (STATUS_CLASS[s] || 'bg-neutral') }, s.replace(/_/g, ' ')); }
  function errText(e){ return e.message + (e.details ? ' ' + JSON.stringify(e.details) : ''); }
  function say(t){ setText('iv-msg', t); }
  function snap(o){ return Object.entries(o || {}).filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object').slice(0, 6).map(([k, v]) => k + ': ' + v).join(' · '); }

  if(!LS_API.available){ setChildren($('iv-list'), na('Not available: investigations are stored by the local LedgerSpy service (`ledgerspy serve`).')); return; }

  /* ---------------- cases & list ---------------- */
  async function loadCases(){
    try {
      const items = (await LS_API.get('/api/datasets')).items;
      const seen = {};
      ST.cases = items.filter(d => !seen[d.case_id] && (seen[d.case_id] = true)).map(d => ({ case_id: d.case_id, name: d.case_name }));
      ST.vocab = ST.vocab || await LS_API.get('/api/investigations/vocabulary');
    } catch(e){ setChildren($('iv-list'), na('Could not load: ' + errText(e))); return; }
    if(!ST.caseId && ST.cases.length) ST.caseId = ST.cases[0].case_id;
    renderControls();
    loadList();
  }
  function renderControls(){
    const cs = select('iv-case', ST.cases.map(c => [c.case_id, c.name]), ST.caseId, 'Engagement case');
    const st = select('iv-status', [['', 'all statuses']].concat(ST.vocab.statuses.map(s => [s, s.replace(/_/g, ' ')])), ST.status, 'Status');
    cs.addEventListener('change', () => { ST.caseId = cs.value; loadList(); });
    st.addEventListener('change', () => { ST.status = st.value; loadList(); });
    setChildren($('iv-controls'), el('div', { class: 'di-toolbar' }, [muted('Engagement case'), cs, muted('Status'), st,
      button('New investigation', () => showCreate(null), true, 'iv-new')]));
  }
  async function loadList(){
    if(!ST.caseId){ setChildren($('iv-list'), na('No engagement case yet: import a ledger first.')); return; }
    try {
      const p = await LS_API.get('/api/investigations' + LS_API.qs({ case_id: ST.caseId, status: ST.status, size: 100 }));
      setChildren($('iv-list'), [muted(lsInt(p.total) + ' investigation(s) in this engagement case'),
        table(['Title', 'Status', 'Priority', 'Outcome', 'Updated', ''], p.items.map(x => [x.title, badge(x.status), x.priority, x.outcome ? x.outcome.replace(/_/g, ' ') : '—', x.updated_at,
          button('Open', () => openCase(x.investigation_id), false)]))]);
    } catch(e){ setChildren($('iv-list'), na('Could not list investigations: ' + errText(e))); }
  }

  /* ---------------- create (optionally from a finding) ---------------- */
  function showCreate(link){
    const title = input('iv-title', 'Factual title', link ? 'Review of ' + link.label : '');
    const desc = area('iv-desc', 'Concise factual description (what prompted the review)');
    const pr = select('iv-priority', ST.vocab.priorities.map(p => [p, p]), 'medium', 'Priority');
    const reason = input('iv-link-reason', 'Why this finding is linked', link ? 'Opened from ' + link.label : '');
    const msg = el('div', { class: 'ls-muted' });
    setChildren($('iv-detail'), [el('h4', null, link ? 'New investigation from: ' + link.label : 'New investigation'),
      muted('An investigation lives in the engagement case and may reference only that case’s datasets, runs and evidence.'),
      title, desc, el('div', { class: 'di-toolbar', style: 'margin-top:6px' }, [muted('Priority'), pr, link ? reason : null].filter(Boolean)),
      el('div', { class: 'di-toolbar', style: 'margin-top:8px' }, [button('Create', async () => {
        try {
          const body = { case_id: ST.caseId, title: title.value, description: desc.value, priority: pr.value,
            links: link ? [Object.assign({ reason: reason.value }, link.ref)] : [] };
          const x = await LS_API.post('/api/investigations', body);
          await loadList(); openCase(x.investigation_id);
        } catch(e){ msg.textContent = 'Not created: ' + errText(e); }
      }, true, 'iv-create')]), msg]);
  }

  /* ---------------- one investigation ---------------- */
  async function openCase(id, keep){
    let inv, links, notes, decs, act, sum;
    try {
      [inv, links, notes, decs, act, sum] = await Promise.all([LS_API.get('/api/investigations/' + id), LS_API.get('/api/investigations/' + id + '/links?size=500'),
        LS_API.get('/api/investigations/' + id + '/notes?size=500'), LS_API.get('/api/investigations/' + id + '/decisions?size=500'),
        LS_API.get('/api/investigations/' + id + '/activity?size=500'), LS_API.get('/api/investigations/' + id + '/summary')]);
    } catch(e){ setChildren($('iv-detail'), na('Investigation not available: ' + errText(e))); return; }
    ST.current = inv;
    const closed = inv.status === 'closed';
    const linkRows = links.items.map(l => [l.link_type.replace(/_/g, ' '), l.ref_id, snap(l.snapshot),
      l.kind === 'finding' ? el('span', null, [el('span', { class: 'badge bg-neutral' }, 'automated finding'), ' analyst: ' + (l.finding_status || 'new')]) : (l.latest_decision ? l.latest_decision.action.replace(/_/g, ' ') : '—'),
      l.active ? 'linked' : 'removed: ' + l.removal.reason,
      el('span', null, [button('Details', () => showLink(id, l.link_id), false),
        l.active && !closed ? button('Decide', () => showDecide(id, l), false) : null,
        l.active && !closed ? button('Unlink', () => showUnlink(id, l), false) : null].filter(Boolean))]);
    const parts = [
      el('h4', { id: 'iv-title-h' }, inv.title),
      el('div', { class: 'di-toolbar' }, [badge(inv.status), muted('priority ' + inv.priority), inv.outcome ? muted('outcome: ' + inv.outcome.replace(/_/g, ' ')) : null, muted(inv.investigation_id)].filter(Boolean)),
      el('p', { id: 'iv-desc-p' }, inv.description || '(no description)'),
      inv.conclusion ? el('div', { class: 't-card' }, [el('strong', null, 'Conclusion (analyst): '), el('span', null, inv.conclusion)]) : null,
      inv.closure_rationale ? muted('Closure rationale: ' + inv.closure_rationale) : null,
      muted(inv.note),
      transitionsBar(inv),
      el('h4', null, 'Linked evidence and findings'),
      el('div', { id: 'iv-links' }, table(['Type', 'Reference', 'Snapshot at link time', 'Automated / analyst', 'State', ''], linkRows)),
      closed ? null : addLinkForm(id),
      el('div', { id: 'iv-link-detail' }),
      el('h4', null, 'Analyst decisions (recorded assessments; automated outputs are unchanged)'),
      table(['When', 'By', 'Subject', 'Decision', 'Rationale', 'Unresolved'], decs.items.map(d => [d.created_at, d.created_by, d.link_id || 'case', d.action_text, d.rationale, d.unresolved ? 'yes' : 'no'])),
      closed ? null : el('div', { class: 'di-toolbar' }, [button('Decision on the case', () => showDecide(id, null), false)]),
      el('h4', null, 'Notes'),
      el('div', { id: 'iv-notes' }, notes.items.length ? notes.items.map(n => el('div', { class: 'ls-contrib' }, [muted(n.created_at + ' · ' + n.created_by + (n.link_id ? ' · on ' + n.link_id : '')), el('div', { class: 'iv-note-body' }, n.body)])) : muted('No notes.')),
      closed ? null : noteForm(id),
      el('h4', null, 'Activity (append-only, each entry in the hash-chained audit log)'),
      table(['#', 'When', 'Actor', 'Event', 'Status', 'Audit entry'], act.items.map(e => [e.seq, e.ts, e.actor, e.event.replace(/_/g, ' '),
        e.to_status ? (e.from_status || '—') + ' → ' + e.to_status : '', '#' + e.audit_seq + ' ' + e.audit_hash.slice(0, 12) + '…'])),
      el('div', { class: 'di-toolbar' }, [button('Evidence manifest', () => showManifest(id), false, 'iv-manifest-btn'), muted('summary ' + sum.summary_sha256.slice(0, 12) + '…')]),
      el('div', { id: 'iv-manifest' }),
    ];
    setChildren($('iv-detail'), parts.filter(Boolean));
    if(!keep) say('');
  }
  function transitionsBar(inv){
    const box = el('div', { id: 'iv-transition' });
    const btns = inv.allowed_transitions.map(to => button('→ ' + to.replace(/_/g, ' '), () => {
      const reason = area('iv-t-reason', to === 'closed' || inv.status === 'closed' || inv.status === 'concluded' ? 'Reason (required, ≥ 10 characters)' : 'Reason (optional)');
      const oc = to === 'concluded' ? select('iv-t-outcome', [['', 'choose the outcome…']].concat(ST.vocab.outcomes.map(o => [o, o.replace(/_/g, ' ')])), '', 'Outcome') : null;
      const concl = to === 'concluded' ? area('iv-t-conclusion', 'Conclusion: the factual basis (required, ≥ 10 characters). Inconclusive is a valid outcome.') : null;
      const msg = el('div', { class: 'ls-muted' });
      setChildren(box, [oc, concl, to === 'concluded' ? null : reason, button('Confirm ' + to.replace(/_/g, ' '), async () => {
        try {
          await LS_API.post('/api/investigations/' + inv.investigation_id + '/transitions', to === 'concluded'
            ? { to_status: to, outcome: oc.value || null, conclusion: concl.value } : { to_status: to, reason: reason.value || null });
          await loadList(); openCase(inv.investigation_id);
        } catch(e){ msg.textContent = 'Not changed: ' + errText(e); }
      }, true, 'iv-t-confirm'), msg].filter(Boolean));
    }, false));
    return el('div', null, [el('div', { class: 'di-toolbar' }, [muted('Valid next status:')].concat(btns)), box]);
  }
  function addLinkForm(id){
    const types = Object.keys(ST.vocab.link_types);
    const ty = select('iv-l-type', types.map(t => [t, t.replace(/_/g, ' ')]), 'transaction', 'Link type');
    const ds = input('iv-l-dataset', 'dataset ID (D-…)'), run = input('iv-l-run', 'run ID (RR-…)'), ref = input('iv-l-ref', 'record ID'), reason = input('iv-l-reason', 'reason for linking');
    const msg = el('div', { class: 'ls-muted' });
    return el('div', null, [el('div', { class: 'di-toolbar' }, [muted('Link a record:'), ty, ds, run, ref, reason, button('Link', async () => {
      const spec = ST.vocab.link_types[ty.value];
      try {
        await LS_API.post('/api/investigations/' + id + '/links', { link_type: ty.value, dataset_id: spec.dataset ? ds.value.trim() : null,
          run_id: spec.run ? run.value.trim() : null, ref_id: ref.value.trim(), reason: reason.value });
        openCase(id);
      } catch(e){ msg.textContent = 'Not linked: ' + errText(e); }
    }, false, 'iv-l-add')]), msg]);
  }
  function noteForm(id){
    const body = area('iv-note', 'Factual note');
    const msg = el('div', { class: 'ls-muted' });
    return el('div', null, [body, el('div', { class: 'di-toolbar' }, [button('Add note', async () => {
      try { await LS_API.post('/api/investigations/' + id + '/notes', { body: body.value }); openCase(id); }
      catch(e){ msg.textContent = 'Not added: ' + errText(e); }
    }, false, 'iv-note-add')]), msg]);
  }
  function showUnlink(id, l){
    const reason = input('iv-u-reason', 'reason for removing the link (the record itself is not changed)');
    const msg = el('div', { class: 'ls-muted' });
    setChildren($('iv-link-detail'), [el('h4', null, 'Unlink ' + l.link_type + ' ' + l.ref_id), reason, button('Unlink', async () => {
      try { await LS_API.post('/api/investigations/' + id + '/links/' + l.link_id + '/unlink', { reason: reason.value }); openCase(id); }
      catch(e){ msg.textContent = 'Not unlinked: ' + errText(e); }
    }, true, 'iv-u-confirm'), msg]);
  }
  async function showDecide(id, l){
    const actions = l ? l.allowed_actions : ST.vocab.actions_by_kind.case;
    const act = select('iv-d-action', [['', 'choose a decision…']].concat(actions.map(a => [a, ST.vocab.action_text[a] || a])), '', 'Decision');
    const why = area('iv-d-rationale', 'Factual rationale (required)');
    const extra = el('div');
    const msg = el('div', { class: 'ls-muted' });
    let chosen = null;
    if(l && l.link_type === 'reconciliation_result'){
      try {
        const d = await LS_API.get('/api/investigations/' + id + '/links/' + l.link_id);
        const r = await LS_API.get('/api/reconciliation/runs/' + l.run_id + '/results/' + l.ref_id);
        if(r.result.status === 'ambiguous'){
          const opts = r.result.candidates.map((c, i) => [String(i), c.type === 'pair' ? c.ledger_txn_id + ' ↔ ' + c.bank_txn_id + ' (score ' + lsScore(c.evidence_score) + ')' : c.settlement + ' ↔ ' + c.components.join('+')]);
          chosen = select('iv-d-candidate', [['', 'resolve to candidate…']].concat(opts), '', 'Candidate');
          chosen._cands = r.result.candidates; chosen._result = r.result;
          setChildren(extra, [muted('Ambiguous result: none was selected automatically. Choosing a candidate records your resolution with its justification; the run itself is unchanged.'), chosen]);
        } else { setChildren(extra, muted('Result status: ' + r.result.status + (d.verification ? ' · link ' + d.verification.state : ''))); }
      } catch(e){ setChildren(extra, muted('Result not available (' + errText(e) + '): the run may be stale.')); }
    }
    let corr = null;
    if(l && l.link_type === 'pdf_item'){
      const f = select('iv-c-field', [['', 'field…']].concat(Object.keys((l.snapshot && l.snapshot.fields) || {}).map(k => [k, k + ' (extracted: ' + l.snapshot.fields[k] + ')'])), '', 'Field');
      const v = input('iv-c-value', 'corrected value as shown on the page');
      corr = { f, v };
      setChildren(extra, [muted('OCR correction: the original extracted value is kept; the correction is an overlay. After confirmation it is a logged P4 transformation and dependent reconciliation runs become stale.'),
        el('div', { class: 'di-toolbar' }, [f, v, button('Record correction', async () => {
          try {
            await LS_API.post('/api/investigations/' + id + '/ocr-corrections', { dataset_id: l.dataset_id, item_id: l.ref_id, field: f.value, value: v.value, rationale: why.value });
            openCase(id);
          } catch(e){ msg.textContent = 'Not corrected: ' + errText(e); }
        }, false, 'iv-c-save')])]);
    }
    setChildren($('iv-link-detail'), [el('h4', null, 'Decision on ' + (l ? l.link_type.replace(/_/g, ' ') + ' ' + l.ref_id : 'the case')), act, why, extra,
      button('Record decision', async () => {
        const body = { link_id: l ? l.link_id : null, action: act.value, rationale: why.value, payload: {} };
        if(act.value === 'ambiguity_resolved' && chosen){
          const c = chosen._cands[Number(chosen.value)];
          if(!c){ msg.textContent = 'Choose the candidate first.'; return; }
          body.payload.chosen = c.type === 'pair' ? { ledger_txn_ids: [c.ledger_txn_id], bank_txn_ids: [c.bank_txn_id] }
            : (chosen._result.bank_txn_ids.indexOf(c.settlement) >= 0 ? { bank_txn_ids: [c.settlement], ledger_txn_ids: c.components } : { ledger_txn_ids: [c.settlement], bank_txn_ids: c.components });
        }
        try { await LS_API.post('/api/investigations/' + id + '/decisions', body); openCase(id); }
        catch(e){ msg.textContent = 'Not recorded: ' + errText(e); }
      }, true, 'iv-d-save'), msg]);
  }
  async function showLink(id, linkId){
    const box = $('iv-link-detail');
    setChildren(box, na('Resolving the referenced record…'));
    try {
      const d = await LS_API.get('/api/investigations/' + id + '/links/' + linkId);
      const canvas = el('canvas', { id: 'iv-page-canvas', width: '10', height: '10', style: 'max-width:100%;display:none;border:1px solid var(--border)' });
      const page = d.link_type === 'pdf_page' ? Number(d.ref_id) : d.link_type === 'pdf_item' ? d.snapshot.page : null;
      setChildren(box, [el('h4', null, d.link_type.replace(/_/g, ' ') + ' ' + d.ref_id),
        muted('Reason: ' + d.reason + (d.annotation ? ' · ' + d.annotation : '') + ' · linked by ' + d.created_by + ' at ' + d.created_at),
        muted('Now: ' + d.verification.state + (d.verification.reason ? ' — ' + d.verification.reason : '') + ' · snapshot sha256 ' + d.snapshot_sha256.slice(0, 16) + '…'),
        el('pre', { class: 'ls-muted', style: 'white-space:pre-wrap' }, JSON.stringify(d.snapshot, null, 2)),
        d.decisions.length ? table(['When', 'Decision', 'Rationale'], d.decisions.map(x => [x.created_at, x.action_text, x.rationale])) : muted('No decision on this link yet.'),
        page ? button('Show PDF page ' + page, async () => {
          try {
            const bmp = await createImageBitmap(await LS_API.blob('/api/datasets/' + d.dataset_id + '/pdf/pages/' + page + '/image?dpi=110'));
            canvas.width = bmp.width; canvas.height = bmp.height; canvas.style.display = 'block'; canvas.getContext('2d').drawImage(bmp, 0, 0);
            if(d.link_type === 'pdf_item' && d.snapshot.bbox){ const k = 110 / 72, b = d.snapshot.bbox; const c = canvas.getContext('2d'); c.strokeStyle = '#ef4444'; c.lineWidth = 2; c.strokeRect(b[0] * k - 3, b[1] * k - 3, (b[2] - b[0]) * k + 6, (b[3] - b[1]) * k + 6); }
          } catch(e){ say('Page not available: ' + errText(e)); }
        }, false, 'iv-page-btn') : null, canvas].filter(Boolean));
    } catch(e){ setChildren(box, na('Link not available: ' + errText(e))); }
  }
  async function showManifest(id){
    try {
      const m = await LS_API.get('/api/investigations/' + id + '/manifest');
      setChildren($('iv-manifest'), [muted('Manifest sha256 ' + m.manifest_sha256 + ' · ' + m.links.length + ' link(s) · verified at ' + m.verified_at),
        table(['Evidence file sha256', 'Verification now'], m.evidence_verification.map(f => [f.sha256, f.verification]))]);
    } catch(e){ setChildren($('iv-manifest'), na('Manifest not available: ' + errText(e))); }
  }

  /* ---------------- entry points from other tabs ---------------- */
  async function start(link){
    try {
      let ds = link.ref.dataset_id;
      if(!ds && link.ref.run_id) ds = (await LS_API.get('/api/reconciliation/runs/' + link.ref.run_id)).datasets.ledger.dataset_id;
      ST.caseId = (await LS_API.get('/api/datasets/' + ds)).case_id;
    } catch(e){ say('Cannot open an investigation: ' + errText(e)); return; }
    const btn = [...document.querySelectorAll('.tab-btn')].find(b => /Investigations/.test(b.textContent));
    if(btn) switchTab('tab-iv', btn);
    await loadCases();
    showCreate(link);
    say('Create an investigation from this finding, or open an existing one and link it there.');
  }
  window.LS_INV = {
    button: (ref, label) => button('Open investigation from this finding', () => start({ ref: ref, label: label }), false),
  };
  loadCases();
})();

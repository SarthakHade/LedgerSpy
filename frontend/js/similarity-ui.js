/* ============================================================
   LedgerSpy — P12 entity similarity matrix (renders the backend resolution)
   ============================================================
   Boundary: records, candidate pairs, statuses, name similarities,
   evidence scores, identifier / address comparisons, conflicts and the
   shared payment patterns come from /api/datasets/{id}/resolution… only.
   Nothing is matched, scored or merged here. A blank matrix cell means
   "no candidate pair in this view" (blocking did not compare the two
   records, or the pair is outside the filter / page), never a similarity
   of zero. All data-derived text is set with textContent (el()/setText()).
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('re-sim-card')) return;

  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 50;
  const MAX_RECORDS = 16;  // presentational cap on the matrix axes; the pair table lists every pair of the page
  // Display only: the status is the API's P12 decision; nothing here decides it.
  const STATUS = Object.freeze({
    conflicting_identity: ['bg-red', 'CONFLICTING IDENTITY'],
    shared_attribute:     ['bg-red', 'SHARED ATTRIBUTE'],
    probable_match:       ['bg-amber', 'PROBABLE MATCH'],
    ambiguous:            ['bg-amber', 'AMBIGUOUS'],
    confirmed_match:      ['bg-green', 'CONFIRMED MATCH'],
    no_match:             ['bg-neutral', 'NO MATCH'],
  });
  const FILTERS = [['', 'Review queue and shared links'], ['conflicting_identity', 'Conflicting identity'],
    ['probable_match', 'Probable match'], ['ambiguous', 'Ambiguous'], ['shared_attribute', 'Shared attribute'],
    ['confirmed_match', 'Confirmed match (merged)'], ['no_match', 'No match']];
  const ST = { datasetId: null, status: '', q: '', page: 1, data: null, selected: null };

  function base(){ return '/api/datasets/' + ST.datasetId + '/resolution'; }
  function muted(t){ return el('div', { class: 'ls-muted' }, t); }
  function badge(status){
    const s = STATUS[status] || ['bg-neutral', String(status).toUpperCase()];
    return el('span', { class: 'badge ' + s[0] }, s[1]);
  }
  function kv(obj){
    const dl = el('dl', { class: 'ls-kv' });
    Object.entries(obj).forEach(([k, v]) => {
      dl.appendChild(el('dt', null, k));
      dl.appendChild(el('dd', null, v instanceof Node ? v : (v === null || v === undefined || v === '' ? '—' : String(v))));
    });
    return dl;
  }
  function list(items){
    return items.length ? el('ul', { class: 'ls-list' }, items.map(t => el('li', null, t))) : muted('None.');
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
  function side(x){ return x.name + ' (' + x.entity_type + ')'; }

  /* ---------------- controls ---------------- */
  function renderControls(){
    const sel = el('select', { class: 'col-filter-select', id: 'sim-status', 'aria-label': 'Pair status' },
      FILTERS.map(([v, t]) => el('option', { value: v }, t)));
    sel.value = ST.status;
    const q = el('input', { type: 'search', id: 'sim-q', class: 'di-reason', placeholder: 'Name contains…', value: ST.q, maxlength: '100' });
    const apply = el('button', { class: 'btn btn-outline di-btn', id: 'sim-apply', type: 'button' }, 'Apply');
    apply.addEventListener('click', () => { ST.status = sel.value; ST.q = q.value.trim(); ST.page = 1; loadPairs(); });
    setChildren($('re-sim-controls'), [sel, q, apply]);
  }
  function renderPager(p){
    const pages = Math.max(1, Math.ceil(p.total / p.size));
    const prev = el('button', { class: 'btn btn-outline di-btn', type: 'button' }, 'Previous');
    const next = el('button', { class: 'btn btn-outline di-btn', type: 'button' }, 'Next');
    prev.disabled = ST.page <= 1; next.disabled = ST.page >= pages;
    prev.addEventListener('click', () => { ST.page -= 1; loadPairs(); });
    next.addEventListener('click', () => { ST.page += 1; loadPairs(); });
    return el('div', { class: 'di-toolbar', id: 'sim-pager', style: 'margin-top:8px' },
      [prev, el('span', { class: 'ls-muted', id: 'sim-page-label' }, 'Page ' + ST.page + ' of ' + pages + ' · ' + lsInt(p.total) + ' pairs'), next]);
  }

  /* ---------------- matrix: rows / columns = records of this page's pairs ---------------- */
  function renderMatrix(items){
    const keys = [], names = {};
    items.forEach(p => [p.a, p.b].forEach(x => { if(!(x.record_key in names)){ keys.push(x.record_key); names[x.record_key] = side(x); } }));
    const shown = keys.slice(0, MAX_RECORDS);
    const byPair = {};
    items.forEach(p => { byPair[p.a.record_key + '\u0000' + p.b.record_key] = p; byPair[p.b.record_key + '\u0000' + p.a.record_key] = p; });
    if(!shown.length){
      setChildren($('re-sim-matrix'), muted('No candidate pair in this view, so there is no matrix to draw.'));
      return;
    }
    const head = el('tr', null, [el('th', null, '#'), el('th', null, 'Record')].concat(shown.map((k, j) => el('th', { title: names[k] }, String(j + 1)))));
    const body = shown.map((ka, i) => el('tr', null, [el('th', null, String(i + 1)), el('th', { class: 'sim-name' }, names[ka])].concat(shown.map(kb => {
      if(ka === kb) return el('td', { class: 'sim-cell sim-self' }, '·');
      const p = byPair[ka + '\u0000' + kb];
      if(!p) return el('td', { class: 'sim-cell sim-none', title: 'No candidate pair in this view' }, '');
      const td = el('td', { class: 'sim-cell ' + (STATUS[p.status] || ['bg-neutral'])[0], 'data-pair-id': p.pair_id,
        title: names[ka] + ' ↔ ' + names[kb] + ': ' + p.status + ', name similarity ' + lsShare(p.name_similarity) + ', evidence score ' + lsScore(p.evidence_score) },
        lsShare(p.name_similarity, 0));
      td.addEventListener('click', () => showPair(p.pair_id));
      return td;
    }))));
    setChildren($('re-sim-matrix'), [
      el('div', { class: 'tx-table-wrap' }, el('table', { class: 'tx-table sim-matrix', id: 'sim-matrix-table' }, [el('thead', null, head), el('tbody', null, body)])),
      muted('Cell = name similarity of the candidate pair (API), coloured by its P12 status; click a cell for the evidence. A blank cell is not a similarity of zero: '
        + 'the two records were not compared (blocking) or the pair is outside this view.'
        + (keys.length > shown.length ? ' Axes show the first ' + shown.length + ' of ' + keys.length + ' records on this page; every pair is in the table below.' : '')),
    ]);
  }

  /* ---------------- pair table and detail ---------------- */
  function renderPairs(p){
    setChildren($('re-sim-pairs'), [
      table(['Status', 'Record A', 'Record B', 'Name similarity', 'Evidence score', 'Rule', 'Conflicts'],
        p.items.map(x => [badge(x.status), side(x.a), side(x.b), lsShare(x.name_similarity), lsScore(x.evidence_score), x.rule, x.conflicts.join('; ') || '—']),
        i => showPair(p.items[i].pair_id)),
      renderPager(p),
    ]);
  }
  function behaviourText(b){
    const m = b.jaccard !== undefined ? 'overlap ' + lsShare(b.jaccard) : (b.ratio !== undefined ? 'ratio ' + lsShare(b.ratio) : '');
    return b.label + (m ? ' (' + m + ')' : '') + (b.shared && b.shared.length ? ': ' + b.shared.join(', ') : '');
  }
  async function showPair(pairId){
    ST.selected = pairId;
    setChildren($('re-sim-detail'), el('div', { class: 'na-inline' }, 'Loading pair ' + pairId + '…'));
    try {
      const d = await LS_API.get(base() + '/pairs/' + pairId);
      if(ST.selected !== pairId) return;
      const comps = Object.entries(d.name.components || {}).map(([k, v]) => k.replace(/_/g, ' ') + ' ' + lsShare(v));
      const ids = Object.entries(d.identifiers || {}).map(([k, v]) => k.replace(/_/g, ' ') + ': ' + String(v).replace(/_/g, ' '));
      const addr = d.address ? 'similarity ' + (d.address.similarity === null ? '—' : lsShare(d.address.similarity)) + ' · postal code ' + d.address.postal_code
        + ' · city ' + d.address.city + (d.address.same_address ? ' · same address' : '') : 'not compared (no address on both records)';
      setChildren($('re-sim-detail'), [
        el('h4', { id: 'sim-detail-title' }, 'Pair ' + d.pair_id),
        el('div', { class: 'ls-headline' }, d.explanation),
        kv({ 'Status': badge(d.status), 'Rule': d.rule, 'Suggested action': d.suggested_action,
          'Evidence score': lsScore(d.evidence_score) + ' (strength of identity evidence, not a fraud probability)',
          'Name similarity': lsShare(d.name.name_similarity) + ' — ' + (comps.join(' · ') || '—'),
          'Normalised names': d.name.normalized.join('  ↔  '), 'Identifiers': ids.join(' · ') || '—', 'Address': addr }),
        el('h4', null, 'Evidence'), list(d.evidence),
        el('h4', null, 'Conflicts and differences'), list(d.conflicts),
        el('h4', null, 'Shared payment patterns (supporting only; never decides the status)'),
        list((d.supporting_behaviour || []).map(behaviourText)),
        el('h4', null, 'Records'),
        table(['Record', 'Source', 'Type', 'Status', 'Ledger rows / master row'], d.records.map(r => [r.name, r.source, r.entity_type, r.record_status,
          r.source === 'ledger' ? lsInt(r.transactions) + ' rows' : r.master_file_id + ' row ' + r.source_row])),
        muted('Identity evidence for review: a pair is merged only on a confirmed match. Record an analyst decision through an investigation (Investigations tab).'),
      ]);
    } catch(e){
      setChildren($('re-sim-detail'), el('div', { class: 'na-inline' }, 'Pair not available: ' + e.message));
    }
  }

  /* ---------------- load ---------------- */
  async function loadPairs(){
    setChildren($('re-sim-pairs'), el('div', { class: 'na-inline' }, 'Loading candidate pairs…'));
    try {
      const p = await LS_API.get(base() + '/pairs' + LS_API.qs({ page: ST.page, size: PAGE_SIZE, status: ST.status || undefined, q: ST.q || undefined }));
      ST.data = p;
      renderMatrix(p.items);
      renderPairs(p);
    } catch(e){
      setChildren($('re-sim-matrix'), null);
      setChildren($('re-sim-pairs'), el('div', { class: 'na-inline' }, 'Candidate pairs not available: ' + e.message));
    }
  }
  async function load(datasetId){
    Object.assign(ST, { datasetId: datasetId, status: '', q: '', page: 1, selected: null });
    setChildren($('re-sim-detail'), el('div', { class: 'na-inline' }, 'Select a matrix cell or a pair.'));
    setChildren($('re-sim-summary'), el('div', { class: 'na-inline' }, 'Resolving entities…'));
    try {
      const [s, ds] = await Promise.all([LS_API.get(base()), LS_API.get('/api/datasets/' + datasetId)]);
      const st = s.stats, by = st.pairs_by_status;
      // Master files are imported with the CLI (installation guide §4), which needs this case ID.
      const hint = (st.records_by_source.master ? 'Master data imported for case ' : 'No vendor / employee master is imported for case ')
        + ds.case_id + '. Import one with: ledgerspy entities import-master --workspace <this workspace> --case-id ' + ds.case_id
        + ' --file <master.csv> --entity-type vendor|employee|customer|group_company (installation guide §4).';
      setChildren($('re-sim-summary'), [
        muted(lsInt(st.records) + ' records (' + Object.entries(st.records_by_source).map(([k, v]) => lsInt(v) + ' ' + k).join(', ') + ') · '
          + lsInt(st.candidate_pairs) + ' candidate pairs compared · review queue ' + lsInt(s.review_queue) + ' · '
          + Object.keys(STATUS).map(k => k.replace(/_/g, ' ') + ' ' + lsInt(by[k])).join(' · ') + ' · resolution ' + s.resolution_sha256.slice(0, 12) + '…'),
        ...Object.values(s.unavailable || {}).map(t => muted('Not available: ' + t + '.')),
        el('div', { class: 'ls-muted', id: 'sim-master-hint' }, hint),
        muted('Merge policy: ' + s.policy.merged + '; never merged: ' + s.policy.never_merged.join(', ').replace(/_/g, ' ') + '. ' + s.policy.note),
      ]);
      renderControls();
      await loadPairs();
    } catch(e){
      setChildren($('re-sim-summary'), el('div', { class: 'na-inline' }, 'Entity resolution not available: ' + e.message));
    }
  }

  // Resolved lazily: only while the Relationships tab is open, so an analysis elsewhere does not wait for it.
  let pending = null;
  function tabOpen(){ const t = $('tab-re'); return !!t && t.classList.contains('active'); }
  document.addEventListener('ls:dataset', ev => {
    if(!LS_API.available) return;
    if(tabOpen()){ pending = null; load(ev.detail.datasetId); }
    else {
      pending = ev.detail.datasetId;
      setChildren($('re-sim-summary'), el('div', { class: 'na-inline' }, 'Entity resolution loads when this tab is open.'));
    }
  });
  document.querySelectorAll('button.tab-btn[data-tab="tab-re"]').forEach(b => b.addEventListener('click', () => {
    if(pending){ const id = pending; pending = null; load(id); }
  }));
})();

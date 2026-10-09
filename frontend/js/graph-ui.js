/* ============================================================
   LedgerSpy — P13 relationship graph view (renders the backend graph)
   ============================================================
   Boundary: nodes, edges, amounts, counts, metrics, cycles, findings
   and signals come from /api/datasets/{id}/graph… only. Nothing is
   computed here except the drawing: node positions are a presentational
   ring layout (by type, then exposure) and are not data. Node size =
   exposure (documented API metric); intensity = PageRank percentile.
   All data-derived text is set with textContent (el()/setText()).
   ============================================================ */
(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('re-graph-card')) return;

  const $ = id => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const W = 760, H = 560;
  const EDGE_TYPES = ['paid', 'shares_bank_account', 'shares_tax_id', 'shares_contact', 'shares_address',
                      'possible_same_entity', 'potential_conflict_of_interest'];
  const EDGE_LABEL = {
    paid: 'Payments', shares_bank_account: 'Shared bank account', shares_tax_id: 'Shared tax ID',
    shares_contact: 'Shared contact', shares_address: 'Shared address', possible_same_entity: 'Possible same entity (P12 review)',
    potential_conflict_of_interest: 'Employee–organisation conflict',
  };
  const EDGE_COLOR = {
    paid: 'var(--cyan)', shares_bank_account: 'var(--red)', shares_tax_id: 'var(--red)', shares_contact: 'var(--amber)',
    shares_address: 'var(--amber)', possible_same_entity: 'var(--txt3)', potential_conflict_of_interest: 'var(--red)',
  };
  const TYPE_RING = ['group_company', 'vendor_customer', 'vendor', 'customer', 'payer_payee', 'payee', 'payer', 'employee', 'account', 'unknown'];
  const ST = { datasetId: null, from: '', to: '', types: new Set(EDGE_TYPES), viz: null, selected: null, cycle: null };

  function base(){ return '/api/datasets/' + ST.datasetId + '/graph'; }
  function windowQs(extra){ return LS_API.qs(Object.assign({ from: ST.from, to: ST.to }, extra || {})); }
  function svg(tag, attrs){
    const n = document.createElementNS(SVGNS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, String(v)));
    return n;
  }
  /** Integer minor units → exact decimal text (no floating point), with the currency. */
  function minor(m, currency){
    if(m === null || m === undefined) return '—';
    const neg = m < 0, a = Math.abs(m);
    return (neg ? '-' : '') + lsAmount(Math.floor(a / 100) + '.' + String(a % 100).padStart(2, '0'), currency);
  }
  function perCurrency(obj){
    const e = Object.entries(obj || {});
    return e.length ? e.map(([c, v]) => minor(v, c === 'unknown' ? null : c)).join(' + ') : '—';
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
  function muted(t){ return el('div', { class: 'ls-muted' }, t); }

  /* ---------------- controls ---------------- */
  function renderControls(){
    const from = el('input', { type: 'date', id: 'gr-from', value: ST.from });
    const to = el('input', { type: 'date', id: 'gr-to', value: ST.to });
    const boxes = EDGE_TYPES.map(t => {
      const cb = el('input', { type: 'checkbox', 'data-edge-type': t });
      cb.checked = ST.types.has(t);
      return el('label', { class: 'ls-muted', style: 'margin-right:12px;white-space:nowrap' }, [cb, ' ' + EDGE_LABEL[t]]);
    });
    const apply = el('button', { class: 'btn btn-outline', id: 'gr-apply', type: 'button' }, 'Apply');
    apply.addEventListener('click', () => {
      ST.from = from.value; ST.to = to.value;
      ST.types = new Set([...document.querySelectorAll('#re-graph-controls input[data-edge-type]')].filter(c => c.checked).map(c => c.dataset.edgeType));
      load(ST.datasetId);
    });
    setChildren($('re-graph-controls'), [
      el('div', { style: 'margin-bottom:8px' }, ['Time window: ', from, ' to ', to, ' ', apply]),
      el('div', null, boxes),
    ]);
  }

  /* ---------------- drawing ---------------- */
  function layout(nodes){
    const groups = {};
    nodes.forEach(n => { const k = TYPE_RING.indexOf(n.node_type); (groups[k < 0 ? 99 : k] = groups[k < 0 ? 99 : k] || []).push(n); });
    const pos = {};
    const centre = groups[0] || [];
    centre.forEach((n, i) => { const a = 2 * Math.PI * i / Math.max(1, centre.length); pos[n.node_id] = [W / 2 + (centre.length > 1 ? 40 : 0) * Math.cos(a), H / 2 + (centre.length > 1 ? 40 : 0) * Math.sin(a)]; });
    const rest = Object.keys(groups).map(Number).filter(k => k !== 0).sort((a, b) => a - b).flatMap(k => groups[k]);
    const rings = [[0.42, 40], [0.64, 90], [0.86, Infinity]];
    let i = 0;
    rings.forEach(([rad, cap]) => {
      const take = rest.slice(i, i + (cap === Infinity ? rest.length : cap));
      take.forEach((n, j) => { const a = 2 * Math.PI * j / take.length - Math.PI / 2; pos[n.node_id] = [W / 2 + rad * (W / 2 - 20) * Math.cos(a), H / 2 + rad * (H / 2 - 20) * Math.sin(a)]; });
      i += take.length;
    });
    return pos;
  }
  function exposure(n){ return Object.values(n.size_metric.value || {}).reduce((s, v) => s + v, 0); }
  function draw(v){
    const host = $('re-graph-canvas');
    if(!v.nodes.length){ setChildren(host, el('div', { class: 'na-inline' }, 'No nodes in this window: the graph is empty, not hidden.')); return; }
    const pos = layout(v.nodes);
    const maxExp = Math.max(1, ...v.nodes.map(exposure));
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', role: 'img', 'aria-label': 'Relationship graph', id: 're-graph-svg' });
    const cycleEdges = new Set();
    if(ST.cycle){ const p = ST.cycle.path; p.forEach((a, k) => cycleEdges.add(a + '>' + p[(k + 1) % p.length])); }
    v.edges.forEach(e => {
      const a = pos[e.source], b = pos[e.target];
      if(!a || !b) return;
      const hot = cycleEdges.has(e.source + '>' + e.target);
      const line = svg('line', { x1: a[0].toFixed(1), y1: a[1].toFixed(1), x2: b[0].toFixed(1), y2: b[1].toFixed(1),
        stroke: hot ? 'var(--amber)' : EDGE_COLOR[e.type], 'stroke-width': hot ? 3 : (e.type === 'paid' ? 1 : 1.5),
        'stroke-opacity': hot ? 1 : (e.type === 'paid' ? 0.25 : 0.8), 'stroke-dasharray': e.type === 'paid' ? '' : '4 3',
        class: 'gr-edge', 'data-edge-id': e.edge_id });
      const t = svg('title'); t.textContent = EDGE_LABEL[e.type] + (e.type === 'paid' ? ': ' + lsInt(e.transactions) + ' payments, ' + minor(e.amount_minor, e.currency) : '');
      line.appendChild(t);
      root.appendChild(line);
    });
    v.nodes.forEach(n => {
      const p = pos[n.node_id];
      const r = 4 + 14 * Math.sqrt(exposure(n) / maxExp);
      const tier = n.p9_risk ? n.p9_risk.tier : null;
      const fill = { critical: 'var(--red)', high: 'var(--red)', medium: 'var(--amber)', low: 'var(--green)' }[tier] || 'var(--txt3)';
      const c = svg('circle', { cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: r.toFixed(1), fill,
        'fill-opacity': (0.35 + 0.65 * (n.intensity_metric.value || 0)).toFixed(2),
        stroke: n.signals.length ? 'var(--red)' : (n.in_cycle ? 'var(--amber)' : 'none'), 'stroke-width': 2,
        class: 'gr-node', 'data-node-id': n.node_id, tabindex: 0 });
      const t = svg('title'); t.textContent = n.label + ' (' + n.node_type + ')';
      c.appendChild(t);
      c.addEventListener('click', () => selectNode(n.node_id));
      root.appendChild(c);
    });
    setChildren(host, [root, muted('Layout: rings by node type and exposure (presentational, not data). Size = exposure (in + out flow); '
      + 'opacity = PageRank percentile; colour = P9 entity tier (display only); red outline = graph signal, amber = in a cycle; '
      + 'dashed = shared attribute (a link, never a merge).')]);
  }

  /* ---------------- detail panels ---------------- */
  async function selectNode(id){
    const d = await LS_API.get(base() + '/nodes/' + id + windowQs());
    ST.selected = id;
    const m = d.metrics;
    const parts = [el('h4', null, d.label), kv({
      'Type': d.node_type, 'Identity (P12)': d.resolution_status + ' · ' + d.members + ' record(s)' + (d.has_master_record ? ' · master data' : ''),
      'Payments in / out': lsInt(m.in_txns) + ' / ' + lsInt(m.out_txns), 'Inflow': perCurrency(m.inflow), 'Outflow': perCurrency(m.outflow),
      'Counterparties': lsInt(m.counterparty_diversity), 'Reciprocity': lsShare(m.reciprocity),
      'PageRank percentile': lsShare(m.pagerank_percentile), 'Betweenness percentile': m.betweenness === null ? 'unavailable' : lsShare(m.betweenness_percentile),
      'P9 entity risk (display)': d.p9_risk ? d.p9_risk.tier + ' ' + lsScore(d.p9_risk.score) : '—',
    })];
    const edges = d.neighbourhood.edges;
    parts.push(el('div', { class: 'ls-muted' }, 'Relationships (' + lsInt(d.neighbourhood.edges_total) + (d.neighbourhood.truncated ? ', first ' + edges.length + ' shown' : '') + ') — click a payment edge for its transactions:'));
    parts.push(table(['Type', 'Direction', 'Counterparty', 'Payments', 'Amount', 'Period'], edges.map(e => {
      const other = e.source === id ? e.target : e.source;
      const lbl = (d.neighbourhood.neighbours.find(x => x.node_id === other) || { label: other }).label;
      return [EDGE_LABEL[e.type], e.type === 'paid' ? (e.source === id ? 'pays →' : '← paid by') : '—', lbl, e.type === 'paid' ? lsInt(e.transactions) : '—',
        e.type === 'paid' ? minor(e.amount_minor, e.currency) : (e.status || []).join(', '), e.first_date ? e.first_date + ' … ' + e.last_date : '—'];
    }), i => { if(edges[i].type === 'paid') selectEdge(edges[i].edge_id); else showEvidence(edges[i]); }));
    (d.signals_detail || []).forEach(s => parts.push(el('div', { class: 'na-card', style: 'margin-top:8px' }, [el('strong', null, s.rule_id + ': '), s.explanation, el('div', { class: 'ls-muted' }, 'Limitations: ' + s.limitations.join('; '))])));
    if(d.ghost_vendor_support){
      const g = d.ghost_vendor_support;
      parts.push(el('div', { class: 'na-card', style: 'margin-top:8px' }, [el('strong', null, 'Verification leads (not a finding): '),
        g.indicators.map(x => x.indicator).join(', '), el('div', { class: 'ls-muted' }, 'Obtain: ' + g.evidence_to_obtain.join('; '))]));
    }
    setChildren($('re-graph-detail'), parts);
  }
  function showEvidence(e){
    setChildren($('re-graph-detail'), [el('h4', null, EDGE_LABEL[e.type]), muted((e.limitations || []).join(' · ')),
      table(['P12 pair', 'Status', 'Detail'], (e.evidence.details || []).map(x => [x.pair_id, x.status || x.pair_status, x.value || x.field || (x.conflicts || []).join('; ') || '—']))]);
  }
  async function selectEdge(id){
    const e = await LS_API.get(base() + '/edges/' + id + windowQs());
    setChildren($('re-graph-detail'), [el('h4', null, e.source_label + ' → ' + e.target_label),
      kv({ 'Payments': lsInt(e.transactions), 'Amount': minor(e.amount_minor, e.currency), 'Period': (e.first_date || '—') + ' … ' + (e.last_date || '—') }),
      (e.limitations || []).length ? muted('Limitations: ' + e.limitations.join('; ')) : null,
      table(['Transaction', 'Date', 'Amount', 'Payer', 'Payee'], e.transactions_detail.map(t => [t.txn_id, t.date, minor(t.amount_minor, t.currency), t.payer, t.payee])),
      e.transactions_truncated ? muted('More transactions exist than shown (limit).') : null].filter(Boolean));
  }

  /* ---------------- cycles and findings ---------------- */
  function labelOf(id){ const n = (ST.viz && ST.viz.nodes.find(x => x.node_id === id)); return n ? n.label : id; }
  async function renderCycles(){
    const c = await LS_API.get(base() + '/cycles' + windowQs({ size: 25 }));
    const s = c.summary;
    const note = 'Cycles of ' + s.length_range[0] + '–' + s.length_range[1] + ' parties (' + lsInt(s.cycles) + ' found'
      + (s.truncated ? '; SEARCH TRUNCATED: ' + s.truncation_reason : '; search complete') + '). Only time-ordered cycles that retain value are marked for review; ordinary trade forms cycles too.';
    setChildren($('re-graph-cycles'), [el('h4', null, 'Circular flows'), muted(note),
      table(['Status', 'Parties', 'Path', 'Span (days)', 'Value retained'], c.items.map(x => [x.status, x.length, x.path.map(labelOf).join(' → ') + ' → ' + labelOf(x.path[0]),
        x.span_days, x.value_retention === null ? '—' : lsShare(x.value_retention)]), i => {
        ST.cycle = c.items[i]; draw(ST.viz);
        const x = c.items[i];
        setChildren($('re-graph-detail'), [el('h4', null, 'Cycle ' + x.cycle_id + ' (' + x.status + ')'),
          x.reasons_for_review.length ? muted('Reasons for review: ' + x.reasons_for_review.join('; ')) : null,
          x.best_chain ? table(['Transaction', 'Date', 'Amount', 'Payer', 'Payee'], x.best_chain.map(t => [t.txn_id, t.date, minor(t.amount_minor, t.currency), t.payer, t.payee])) : muted('No time-ordered chain of payments within the window.'),
          muted('Limitations: ' + x.limitations.join('; '))].filter(Boolean));
      })]);
  }
  async function renderFindings(){
    const [ev, gh] = await Promise.all([LS_API.get(base() + '/employee-vendor' + windowQs({ size: 25 })), LS_API.get(base() + '/ghost-vendors' + windowQs({ size: 25 }))]);
    setChildren($('re-graph-findings'), [el('h4', null, 'Employee–vendor evidence'),
      muted('Classification from shared identifiers only; ownership or control is never inferred, and no declared relationship register is ingested.'),
      table(['Classification', 'Employee', 'Organisation', 'Shared', 'Payments to organisation'], ev.items.map(f => [f.classification, f.employee.name, f.organisation.name,
        Object.keys(f.shared).join(', '), lsInt(f.payments_to_organisation.transactions)])),
      el('h4', null, 'Ghost-vendor verification leads'),
      muted(gh.master_data + '. Leads to verify, never a conclusion that a vendor is fictitious.'),
      table(['Counterparty', 'Indicators', 'Payments'], gh.items.map(g => [g.label, g.indicators.map(x => x.indicator).join(', '), lsInt(g.payments.transactions)]), i => selectNode(gh.items[i].node_id))]);
  }

  /* ---------------- load ---------------- */
  async function load(datasetId){
    ST.datasetId = datasetId; ST.cycle = null;
    renderControls();
    setChildren($('re-graph-canvas'), el('div', { class: 'na-inline' }, 'Building the relationship graph…'));
    try {
      const q = '?' + [['from', ST.from], ['to', ST.to]].filter(([, v]) => v).map(([k, v]) => k + '=' + encodeURIComponent(v))
        .concat([...ST.types].map(t => 'edge_types=' + encodeURIComponent(t))).join('&');
      const [s, v] = await Promise.all([LS_API.get(base() + windowQs()), LS_API.get(base() + '/visualization' + q)]);
      ST.viz = v;
      const na = $('re-graph-na');
      if(na) na.hidden = true;
      setText('re-graph-summary', lsInt(s.stats.nodes) + ' nodes · ' + lsInt(s.stats.edges) + ' edges (' + lsInt(s.stats.edges_by_type.paid) + ' payment links) · '
        + lsInt(s.cycles.cycles) + ' cycles · ' + lsInt(s.signals.total) + ' graph signals (separate from P9 scores) · graph ' + s.graph_sha256.slice(0, 12) + '…'
        + (v.truncated.nodes || v.truncated.edges ? ' · VIEW TRUNCATED: showing ' + v.nodes.length + ' of ' + v.truncated.nodes_total + ' nodes, ' + v.edges.length + ' of ' + v.truncated.edges_total + ' edges' : ''));
      draw(v);
      await Promise.all([renderCycles(), renderFindings()]);
    } catch(e){
      setChildren($('re-graph-canvas'), el('div', { class: 'na-inline' }, 'Relationship graph not available: ' + e.message));
    }
  }

  document.addEventListener('ls:dataset', ev => { if(LS_API.available) load(ev.detail.datasetId); });
})();

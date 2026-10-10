/* ============================================================
   LedgerSpy — P9 risk UI (renders the backend analysis)
   ============================================================
   Boundary: every score, tier, count, amount, chart value and
   explanation on the page comes from the LedgerSpy API
   (/api/datasets/{id}/risk…). This file fetches and renders; it never
   computes risk, tiers or statistics. Unavailable modules say so.
   All data-derived text is set with textContent via el()/setText().
   ============================================================ */
const LS_STATE = { datasetId: null, summary: null, detectors: null, page: 1, size: 50, tier: '', q: '', sort: 'priority' };

(function(){
  'use strict';
  if(typeof document === 'undefined' || !document.getElementById('hero')) return;

  const $ = id => document.getElementById(id);
  const LOCKS = ['fpanel-lock', 'tv-lock', 'rp-lock'];
  const SVGNS = 'http://www.w3.org/2000/svg';

  function status(text, tone){
    setText('main-status-text', text);
    const dot = $('main-status-dot');
    if(dot && tone){ dot.style.background = 'var(--' + tone + ')'; dot.style.boxShadow = '0 0 10px var(--' + tone + ')'; }
  }
  function na(text){ return el('div', { class: 'na-card' }, text); }
  function muted(text){ return el('div', { class: 'ls-muted' }, text); }
  function kv(obj){
    const dl = el('dl', { class: 'ls-kv' });
    Object.entries(obj || {}).forEach(([k, v]) => {
      dl.appendChild(el('dt', null, k));
      dl.appendChild(el('dd', null, v === null || v === undefined ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v))));
    });
    return dl;
  }
  function table(headers, rows){
    return el('div', { class: 'tx-table-wrap di-table' }, el('table', { class: 'tx-table' }, [
      el('thead', null, el('tr', null, headers.map(h => el('th', null, h)))),
      el('tbody', null, rows.length ? rows.map(r => el('tr', null, r.map(c => el('td', null, c instanceof Node ? c : (c === null || c === undefined ? '—' : String(c))))))
                                    : el('tr', null, el('td', { colspan: String(headers.length) }, 'None.'))),
    ]));
  }
  function button(label, onClick, primary){
    const b = el('button', { class: primary ? 'btn btn-primary di-btn' : 'btn btn-secondary di-btn', type: 'button' }, label);
    b.addEventListener('click', onClick);
    return b;
  }
  function toast(text){
    setText('toast-msg', text);
    const t = $('import-toast');
    if(!t) return;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
  }
  function cur(){ return LS_STATE.summary ? LS_STATE.summary.amounts.currency : null; }
  function base(){ return '/api/datasets/' + LS_STATE.datasetId + '/risk'; }

  /* ---------------- no backend: say so, compute nothing ---------------- */
  if(!LS_API.available){
    status('Not connected: open LedgerSpy through `ledgerspy serve`. All analysis runs in the local LedgerSpy service; this page only displays its results.', 'red');
    setText('fp-status', 'Backend required');
    setText('hero-findings', 'No results: the local LedgerSpy service is required. Nothing is computed in the browser.');
    ['fi-detectors', 'fi-dataset', 'fi-benford', 'an-detectors', 're-entities', 'di-summary'].forEach(id => setChildren($(id), na('Not available: requires the local LedgerSpy service (`ledgerspy serve`).')));
    return;
  }

  /* ---------------- datasets ---------------- */
  async function loadDatasets(select){
    let items;
    try { items = (await LS_API.get('/api/datasets')).items; }
    catch(e){ status('Could not reach the backend: ' + e.message, 'red'); return; }
    // bank statements (P14) are reconciled in the Bank Recon tab, not analysed as ledgers
    const usable = items.filter(d => d.status === 'canonicalised' && (d.kind || 'ledger') === 'ledger');
    const sel = $('dataset-select');
    setChildren(sel, usable.map(d => el('option', { value: d.dataset_id },
      d.original_filename + ' · ' + d.case_name + ' · ' + lsInt(d.row_count) + ' rows')));
    $('dataset-picker').style.display = usable.length ? 'flex' : 'none';
    $('upload-zone').style.display = 'block';
    if(select && usable.some(d => d.dataset_id === select)) sel.value = select;
    status(usable.length ? usable.length + ' dataset(s) in this workspace. Choose one and run the analysis, or import a ledger.'
                         : 'No dataset in this workspace yet. Import a CSV/XLSX ledger to begin.', 'cyan');
  }

  /* ---------------- import: upload → confirm mapping → integrity ---------------- */
  async function importFile(file){
    status('Uploading ' + file.name + ' to the local service…', 'cyan');
    try {
      const kase = await LS_API.post('/api/cases', { name: 'Import ' + file.name });
      const ds = await LS_API.upload(kase.case_id, file);
      showMapping(ds);
    } catch(e){
      status('Import failed: ' + e.message, 'red');
    }
  }

  function showMapping(ds){
    const ms = ds.mapping_suggestion;
    const proposed = JSON.parse(JSON.stringify(ms.proposed_mapping));
    const card = $('mapping-card');
    card.style.display = 'block';
    setText('mapping-file', ds.evidence.original_filename + ' · ' + lsInt(ds.row_count) + ' rows · evidence sha256 ' + ds.evidence.sha256.slice(0, 16) + '…');
    const rows = Object.entries(proposed.fields).sort().map(([f, c]) => [f, c]);
    const parts = [
      muted('Adapter: ' + proposed.adapter + ' (' + ms.adapter.basis + ')'),
      el('div', { class: 'ls-mapping' }, table(['Canonical field', 'Source column'], rows)),
      ms.label_columns.length ? muted('Label columns stored separately and never used: ' + ms.label_columns.join(', ')) : null,
      ms.unmapped_columns.length ? muted('Unmapped columns kept verbatim (not analysed): ' + ms.unmapped_columns.join(', ')) : null,
    ];
    let dirSelect = null;
    ms.requires_confirmation.forEach(item => {
      parts.push(el('div', { class: 'na-card', style: 'margin-top:8px' }, item.item + ': ' + item.reason));
      if(item.item === 'direction source'){
        dirSelect = el('select', { class: 'col-filter-select', 'aria-label': 'Direction for every row' }, [
          el('option', { value: '' }, 'choose…'),
          el('option', { value: 'debit' }, 'every row is a payment out (debit)'),
          el('option', { value: 'credit' }, 'every row is a receipt in (credit)'),
        ]);
        parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:8px' }, [muted('Direction for every row'), dirSelect]));
      }
    });
    const msg = el('div', { class: 'ls-muted', style: 'margin-top:8px' });
    parts.push(el('div', { class: 'di-toolbar', style: 'margin-top:12px' }, [
      button('Confirm mapping and analyse', async () => {
        if(dirSelect){
          if(!dirSelect.value){ msg.textContent = 'Choose the direction first.'; return; }
          proposed.options.direction = dirSelect.value;
        }
        try {
          msg.textContent = 'Confirming mapping…';
          await LS_API.put('/api/datasets/' + ds.dataset_id + '/mapping', proposed);
          msg.textContent = 'Running the integrity check…';
          await LS_API.post('/api/datasets/' + ds.dataset_id + '/integrity');
          card.style.display = 'none';
          await loadDatasets(ds.dataset_id);
          toast(lsInt(ds.row_count) + ' rows imported as evidence');
          analyse();
        } catch(e){
          msg.textContent = 'Not accepted: ' + e.message + (e.details ? ' ' + JSON.stringify(e.details) : '');
        }
      }, true),
      button('Cancel', () => { card.style.display = 'none'; }),
    ]));
    parts.push(msg);
    setChildren($('mapping-body'), parts.filter(Boolean));
    status('Check the proposed mapping for ' + ds.evidence.original_filename + ' and confirm it.', 'amber');
  }

  /* ---------------- analysis ---------------- */
  async function analyse(){
    const id = $('dataset-select').value;
    if(!id){ status('Choose a dataset first.', 'amber'); return; }
    LS_STATE.datasetId = id;
    LS_STATE.page = 1;
    const btn = $('run-btn');
    btn.disabled = true;
    setText('fp-status', 'Analysing…');
    status('Analysing in the local service (features → forensic rules → anomaly detection → risk fusion)…', 'cyan');
    try {
      const summary = await LS_API.post(base());
      const [detectors, scatter, entities] = await Promise.all([
        LS_API.get(base() + '/detectors'), LS_API.get(base() + '/scatter'), LS_API.get(base() + '/entities' + LS_API.qs({ size: 25 })),
      ]);
      LS_STATE.summary = summary; LS_STATE.detectors = detectors;
      LOCKS.forEach(l => { const n = $(l); if(n) n.classList.remove('lock-blur'); });
      renderHero(summary);
      renderOverview(summary);
      renderIntegritySummary(summary);
      renderFindings(summary, detectors);
      renderTrends(summary, detectors);
      renderScatter(scatter);
      renderAnomalyDetectors(detectors);
      renderEntities(summary, entities);
      renderExplainability(summary);
      renderInsights(summary);
      await loadTable();
      setText('fp-status', 'Backend result');
      status('Analysis finished: ' + lsInt(summary.counts.scored) + ' of ' + lsInt(summary.counts.transactions) + ' transactions scored · result ' + summary.result_sha256.slice(0, 12) + '…', 'green');
      document.dispatchEvent(new CustomEvent('ls:dataset', { detail: { datasetId: id } }));
      if(typeof addChatMsgText === 'function') addChatMsgText('Analysis loaded: ' + lsInt(summary.counts.high_or_critical) + ' high/critical and ' + lsInt(summary.counts.tiers.medium) + ' medium transactions. Try "show summary" or "why is <ID> flagged?".');
    } catch(e){
      status('Analysis failed: ' + e.message, 'red');
      setText('fp-status', 'Error');
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------------- hero ---------------- */
  function renderHero(s){
    const c = s.counts.tiers, g = s.volume_guard;
    setText('hero-n-critical', lsInt(c.critical)); setText('hero-n-high', lsInt(c.high));
    setText('hero-n-medium', lsInt(c.medium)); setText('hero-n-low', lsInt(c.low));
    setText('fg-n', lsInt(g.high_or_critical) + '/' + lsInt(g.high_budget));
    const ring = $('fg-ring');
    if(ring){
      const C = 2 * Math.PI * 54;
      const fill = g.high_budget ? Math.min(g.high_or_critical / g.high_budget, 1) : (g.high_or_critical ? 1 : 0);
      ring.style.strokeDasharray = C; ring.style.strokeDashoffset = C - C * fill;
      ring.style.stroke = g.calibration_warning ? 'var(--red)' : 'var(--cyan)';
    }
    setText('hero-findings', lsInt(s.counts.medium_or_higher) + ' of ' + lsInt(s.counts.scored) + ' scored transactions are medium or higher; ' +
      lsInt(s.counts.high_or_critical) + ' high/critical (' + lsShare(g.high_or_critical_share, 2) + '). ' + lsInt(s.counts.insufficient_data) +
      ' not assessed. ' + lsInt(s.counts.detectors_unavailable) + ' of ' + lsInt(s.counts.detectors_total) + ' detectors unavailable for this data.');
    const loader = $('mini-loader');
    if(loader) loader.style.width = (s.counts.scored ? 100 * s.counts.medium_or_higher / s.counts.scored : 0) + '%';
    const notice = $('risk-notice');
    notice.style.display = 'block';
    notice.classList.toggle('warn', !!g.calibration_warning);
    setText('risk-notice-title', g.calibration_warning ? '⚠ Review volume above budget' : 'Review priority, not probability');
    setText('risk-notice-text', g.message + '. ' + s.method.score);
  }

  /* ---------------- overview ---------------- */
  function renderOverview(s){
    const t = s.counts.tiers, total = s.counts.transactions;
    const segs = [['critical', t.critical], ['high', t.high], ['medium', t.medium], ['low', t.low], ['insufficient', s.counts.insufficient_data]];
    const C = 2 * Math.PI * 80;
    let offset = 0;
    segs.forEach(([name, n]) => {
      const c = $('donut-' + name);
      const len = total ? C * n / total : 0;
      c.setAttribute('stroke-dasharray', len.toFixed(3) + ' ' + C.toFixed(3));
      c.setAttribute('stroke-dashoffset', (-offset).toFixed(3));
      c.setAttribute('data-count', String(n));
      offset += len;
    });
    setText('donut-total', lsInt(total));
    setText('ov-total', lsInt(s.counts.scored) + ' scored');
    setText('leg-critical', '■ Critical ' + lsInt(t.critical)); setText('leg-high', '■ High ' + lsInt(t.high));
    setText('leg-medium', '■ Medium ' + lsInt(t.medium)); setText('leg-low', '■ Low ' + lsInt(t.low));
    setText('leg-insufficient', '■ Not assessed ' + lsInt(s.counts.insufficient_data));
    const a = s.amounts, c = a.currency;
    setText('qs-avg-high', a.high_or_critical_average === null ? '—' : lsAmount(a.high_or_critical_average, c));
    setText('qs-high-amt', a.medium_or_higher === null ? (a.currency ? '—' : 'mixed currencies') : lsAmount(a.medium_or_higher, c));
    setText('qs-flag-rate', lsShare(s.volume_guard.high_or_critical_share, 2) + ' / ' + lsShare(s.volume_guard.max_high_share, 1));
    setText('qs-payees', lsInt(s.entities.with_high_or_critical_transactions));
    setText('exp-val', a.high_or_critical === null ? (a.currency ? '—' : 'mixed currencies') : lsAmount(a.high_or_critical, c));
    setText('exp-desc', 'Sum of |amount| over ' + lsInt(s.counts.high_or_critical) + ' high/critical transactions, of ' +
      (a.total === null ? 'a multi-currency total' : lsAmount(a.total, c)) + ' in the dataset. Amounts at review priority, not losses.');
    const max = Math.max(1, ...s.score_distribution.map(b => b.count));
    setChildren($('ov-histogram'), s.score_distribution.map(b => el('div', { class: 'ls-bar' }, [
      el('span', { class: 'ls-bar-l' }, b.from + '–' + b.to),
      el('span', { class: 'ls-bar-bg' }, el('span', { class: 'ls-bar-fill', style: 'display:block;width:' + (100 * b.count / max).toFixed(2) + '%' })),
      el('span', { class: 'ls-bar-v' }, lsInt(b.count)),
    ])));
  }

  /* ---------------- data integrity (readiness, separate from risk) ---------------- */
  function renderIntegritySummary(s){
    const r = s.readiness;
    if(r.status === 'not_run'){ setChildren($('di-summary'), na(r.note)); return; }
    setChildren($('di-summary'), [
      el('div', { class: 'di-readiness' }, [
        el('div', { class: 'di-score' }, Number(r.score).toFixed(1) + '%'),
        el('div', null, [el('div', { class: 'di-status' }, r.status.replace(/_/g, ' ')), muted(r.issue_count + ' issue(s) · run ' + r.run_id), muted(r.note)]),
      ]),
    ]);
  }

  /* ---------------- findings (P7 + P8 detectors) ---------------- */
  function renderFindings(s, d){
    const rows = d.items.map(i => [i.detector, i.source, i.status === 'evaluated' ? 'evaluated' : i.status.toUpperCase().replace(/_/g, ' '),
      i.reason || '—', i.family_label + (i.scored ? '' : ' (not scored)'), lsInt(i.signals), lsInt(i.transactions_raised)]);
    setChildren($('fi-detectors'), [
      table(['Detector', 'Source', 'Status', 'Why unavailable', 'Risk family', 'Signals', 'Transactions raised'], rows),
      muted('Detectors that cannot run on this data contribute nothing to any score; each says why.'),
    ]);
    setChildren($('fi-dataset'), s.dataset_findings.length
      ? s.dataset_findings.map(f => el('div', { class: 'ls-contrib' }, [
          el('div', { class: 'ls-contrib-h' }, [el('span', null, f.detector + ' · ' + f.kind + ' ' + f.subject_id), el('span', { class: 'ls-contrib-pts' }, lsInt(f.rows_cited) + ' rows cited')]),
          muted(f.explanation)]))
      : muted('No counterparty- or subgroup-level finding for this dataset.'));
    const b = d.benford;
    const tests = Object.entries(b.dataset).map(([test, r]) => [test, r.status, lsInt(r.n), r.mad === undefined ? '—' : r.mad, r.band || '—',
      r.chi2_p_value === undefined ? '—' : r.chi2_p_value, (r.over_represented || []).join(', ') || '—', r.reason || (r.warnings || []).join('; ') || '—']);
    const sub = Object.entries(b.subgroups).map(([dim, v]) => v.status !== 'analyzed' ? [dim, v.status, v.reason, '—', '—']
      : [dim, 'analyzed', 'min n ' + v.tests.first.min_n, lsInt(v.tests.first.groups_analyzed) + ' analysed / ' + lsInt(v.tests.first.groups_skipped_small) + ' skipped (too small)',
         v.tests.first.results.filter(g => g.band === 'nonconformity').map(g => g.group).join(', ') || 'none']);
    setChildren($('fi-benford'), [
      table(['Test', 'Status', 'n', 'MAD', 'Band', 'χ² p-value', 'Over-represented digits', 'Reason / warnings'], tests),
      el('h4', { style: 'margin-top:16px;font-family:var(--mono);font-size:0.75rem;color:var(--txt3)' }, 'SUBGROUPS (first digit)'),
      table(['Dimension', 'Status', 'Gate', 'Groups', 'Nonconforming groups'], sub),
      muted(b.note),
    ]);
  }

  /* ---------------- trends: monthly tiers, Benford chart, top counterparties ---------------- */
  function renderTrends(s, d){
    const box = $('trends-monthly-bars');
    if(s.monthly.status !== 'available'){
      setChildren(box, el('div', { style: 'width:100%;text-align:center;color:var(--txt3);font-size:0.8rem;margin-bottom:100px' }, 'Not available: ' + s.monthly.reason));
      setText('trends-monthly-alert', 'Monthly series need real calendar dates.');
    } else {
      const months = s.monthly.months;
      const max = Math.max(1, ...months.map(m => m.low + m.medium + m.high + m.critical + m.insufficient_data));
      setChildren(box, months.map(m => {
        const total = m.low + m.medium + m.high + m.critical + m.insufficient_data;
        const seg = (n, color) => el('div', { style: 'width:100%;background:' + color + ';height:' + (100 * n / max).toFixed(3) + '%' });
        return el('div', { style: 'flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;min-width:3px', title: m.month + ': ' + total + ' rows, ' + (m.high + m.critical) + ' high+, ' + m.medium + ' medium' },
          [seg(m.high + m.critical, 'var(--red)'), seg(m.medium, 'var(--amber)'), seg(m.low + m.insufficient_data, 'rgba(34,211,238,0.5)')]);
      }));
      setText('trends-monthly-alert', months.length + ' months from ' + months[0].month + ' to ' + months[months.length - 1].month + '. Bar height = transactions in the month; red = high + critical, amber = medium.');
    }
    const first = d.benford.dataset.first;
    const bars = $('benford-bars');
    if(!first || first.status !== 'analyzed'){
      setChildren(bars, el('div', { class: 'na-inline', style: 'align-self:center;width:100%' }, 'Not analysed: ' + (first ? (first.reason || first.status) : 'no result')));
      setText('benford-note', 'The backend Benford test did not run on this data (minimum sample and exclusion rules apply).');
    } else {
      const max = Math.max(...Object.values(first.expected), ...Object.values(first.observed));
      setChildren(bars, Object.keys(first.expected).map(dgt => {
        const exp = first.expected[dgt], obs = first.observed[dgt];
        const over = (first.over_represented || []).map(String).includes(dgt);
        return el('div', { style: 'flex:1;display:flex;flex-direction:column;align-items:center;height:100%;justify-content:flex-end', title: 'digit ' + dgt + ': observed ' + lsShare(obs) + ', expected ' + lsShare(exp) }, [
          el('div', { style: 'width:100%;display:flex;gap:2px;align-items:flex-end;height:100%' }, [
            el('div', { style: 'flex:1;background:var(--cyan);opacity:0.25;height:' + (100 * exp / max).toFixed(2) + '%' }),
            el('div', { style: 'flex:1;background:' + (over ? 'var(--red)' : 'var(--cyan)') + ';height:' + (100 * obs / max).toFixed(2) + '%' }),
          ]),
          el('div', { style: 'font-family:var(--mono);font-size:0.7rem;color:var(--txt3);margin-top:4px' }, dgt),
        ]);
      }));
      setText('benford-note', 'n = ' + lsInt(first.n) + ' · MAD ' + first.mad + ' (' + first.band + ') · χ² p = ' + first.chi2_p_value + '. ' + first.note);
    }
    const top = s.entities.top.filter(e => e.exposure_amount_minor > 0).sort((a, b) => b.exposure_amount_minor - a.exposure_amount_minor).slice(0, 6);
    setChildren($('trends-top-vendors'), top.length ? top.map(e => el('div', { class: 'ls-contrib' }, [
      el('div', { class: 'ls-contrib-h' }, [el('span', null, e.name + ' (' + e.flow + ')'), el('span', { class: 'ls-contrib-pts' }, lsAmount(e.exposure, cur()))]),
      el('div', null, [lsTierBadge(e.tier, e.score), ' ', muted(lsInt(e.transactions_medium_or_higher) + ' of ' + lsInt(e.transactions) + ' rows medium or higher')]),
    ])) : muted('No counterparty has medium-or-higher rows.'));
    setText('trends-total-exposure', s.amounts.medium_or_higher === null ? '—' : lsAmount(s.amounts.medium_or_higher, s.amounts.currency));
    setText('trends-exposure-desc', lsInt(s.counts.medium_or_higher) + ' medium-or-higher transactions (top 10 counterparties by review order are listed in Relationships).');
  }

  /* ---------------- anomalies: scatter of backend points ---------------- */
  function renderScatter(sc){
    const svg = $('scatter-svg');
    svg.querySelectorAll('.scatter-dot').forEach(n => n.remove());
    const pts = sc.points;
    setText('scatter-note', pts.length + ' points: ' + sc.sampling + ' (' + lsInt(sc.zero_score_rows_shown) + ' of ' + lsInt(sc.zero_score_rows) + ' zero-score rows shown).');
    if(!pts.length) return;
    const SW = 760, SH = 310, PAD = 40;
    const logA = p => Math.log10(Math.max(1, Math.abs(p.amount_minor)));
    const logs = pts.map(logA);
    const minLog = Math.min(...logs), maxLog = Math.max(minLog + 1, ...logs);  // axis spans the plotted amounts
    const color = { critical: '#ef4444', high: 'var(--red)', medium: 'var(--amber)', low: 'var(--cyan)' };
    pts.forEach(p => {
      const c = document.createElementNS(SVGNS, 'circle');
      c.setAttribute('cx', (PAD + 10 + (p.score / 100) * (SW - PAD - 20)).toFixed(1));
      c.setAttribute('cy', (SH - 10 - ((logA(p) - minLog) / (maxLog - minLog)) * (SH - 30)).toFixed(1));
      c.setAttribute('r', p.tier === 'low' ? 3 : 5);
      c.setAttribute('fill', color[p.tier] || 'var(--txt3)');
      c.setAttribute('opacity', p.tier === 'low' ? '0.35' : '0.85');
      c.setAttribute('class', 'scatter-dot');
      c.addEventListener('mouseover', e => {
        const tt = $('ano-tt'), box = $('scatter-box').getBoundingClientRect();
        tt.textContent = p.txn_id + ' — ' + p.tier + ' — score ' + lsScore(p.score) + ' — ' + lsAmount(p.amount, cur());
        tt.style.opacity = '1'; tt.style.left = (e.clientX - box.left + 14) + 'px'; tt.style.top = (e.clientY - box.top - 36) + 'px';
      });
      c.addEventListener('mouseout', () => { $('ano-tt').style.opacity = '0'; });
      c.addEventListener('click', async () => {
        $('ano-panel').style.display = 'block';
        setText('ap-id', p.txn_id);
        setChildren($('ap-body'), muted('Loading explanation…'));
        try { setChildren($('ap-body'), renderExplanation(await LS_API.get(base() + '/transactions/' + encodeURIComponent(p.txn_id)))); }
        catch(e){ setChildren($('ap-body'), na('Could not load: ' + e.message)); }
      });
      svg.appendChild(c);
    });
  }

  function renderAnomalyDetectors(d){
    const rows = d.items.filter(i => i.source === 'P8 anomaly detector').map(i => [i.detector, i.status, i.reason || '—', lsInt(i.rows_scored),
      lsInt(i.rows_insufficient), (i.features_used || []).length + ' used / ' + (i.features_excluded || []).length + ' excluded', lsInt(i.signals)]);
    setChildren($('an-detectors'), [table(['Detector', 'Status', 'Reason', 'Rows scored', 'Rows insufficient', 'Features', 'Signals'], rows),
      muted('Population: ' + d.population.mode + ' reference, ' + lsInt(d.population.reference_rows) + ' reference rows' +
            (d.population.warm_up && d.population.warm_up.applied ? '; first ' + d.population.warm_up.days + ' days not scored (warm-up)' : '') +
            '. "Unusual" is relative to this dataset, never "fraudulent".')]);
  }

  /* ---------------- relationships: counterparty risk ---------------- */
  // P12 identity of a counterparty, exactly as the API reports it (no client-side matching or scores).
  const LS_IDENTITY = Object.freeze({
    conflicting_identity: Object.freeze({ cls: 'bg-red',     txt: 'CONFLICTING IDENTITY · review' }),
    needs_review:         Object.freeze({ cls: 'bg-amber',   txt: 'POSSIBLE MATCH · review' }),
    resolved:             Object.freeze({ cls: 'bg-green',   txt: 'RESOLVED' }),
    unique:               Object.freeze({ cls: 'bg-neutral', txt: 'UNIQUE' }),
  });
  function identityCell(r){
    if (!r) return '—';
    const t = LS_IDENTITY[r.resolution_status] || LS_IDENTITY.unique;
    let detail = ' ' + lsInt(r.linked_records) + (r.linked_records === 1 ? ' record' : ' records');
    if (r.merged_streams > 1) detail += ' · ' + lsInt(r.merged_streams) + ' ledger names merged';
    if (r.master_records) detail += ' · master data';
    return el('span', null, [el('span', { class: 'badge ' + t.cls }, t.txt), el('span', { class: 'ls-muted' }, detail)]);
  }
  function renderEntities(s, page){
    setText('rel-stat-total', lsInt(s.entities.total)); setText('rel-stat-medium', lsInt(s.entities.medium_or_higher));
    setText('rel-stat-high', lsInt(s.entities.high_or_critical)); setText('rel-stat-withhigh', lsInt(s.entities.with_high_or_critical_transactions));
    const rows = page.items.map(e => [lsInt(e.review_rank), e.name + ' (' + e.flow + ')', lsTierBadge(e.tier, e.score), identityCell(e.resolution),
      lsInt(e.independent_sources), lsInt(e.transactions_medium_or_higher) + ' / ' + lsInt(e.transactions), lsAmount(e.exposure, e.currency),
      e.entity_signals.map(x => x.detector).join(', ') || '—']);
    const g = s.entity_grouping;
    const grouping = g && g.method === 'resolved'
      ? 'Counterparties are grouped by entity resolution: ledger names are merged only on confirmed matches (' + lsInt(g.merged_entities) + ' merged entities); possible matches and conflicting identities are flagged for review and never merged.'
      : 'Counterparties are grouped by normalised name only.';
    setChildren($('re-entities'), [table(['Rank', 'Counterparty', 'Risk', 'Identity', 'Sources', 'Medium+ rows', 'Exposure', 'Counterparty-level signals'], rows),
      muted('Showing ' + page.items.length + ' of ' + lsInt(page.total) + '. Entity score = noisy-OR of its top transaction scores (each transaction counted once). ' + grouping)]);
  }

  /* ---------------- explainability: method + family summary ---------------- */
  function renderExplainability(s){
    setText('ex-risk-label', lsInt(s.counts.with_signals) + ' rows with signals');
    setText('ex-desc', 'Bars: rows each evidence family raised, and rows where it is the largest contributor ("leads"). Every score is an exact breakdown of these families (not SHAP); open a transaction for its own breakdown.');
    const fams = s.method.families;
    const maxLed = Math.max(1, ...fams.map(f => f.transactions_raised));
    setChildren($('ex-bars'), fams.map(f => el('div', { class: 'fac-row' }, [
      el('div', { class: 'fac-label' }, f.label),
      el('div', { class: 'fac-bar-bg' }, el('div', { class: 'fac-bar-fill', style: 'width:' + (100 * f.transactions_raised / maxLed).toFixed(2) + '%;background:' + (f.scored ? 'var(--amber)' : 'var(--txt3)') })),
      el('div', { class: 'fac-val', style: 'width:auto;min-width:160px;font-size:0.75rem' }, lsInt(f.transactions_raised) + ' rows · leads ' + lsInt(f.transactions_led) + (f.scored ? '' : ' · not scored')),
    ])));
    const t = s.method.tiers;
    setChildren($('ex-method'), [
      el('p', { class: 'ls-headline' }, s.method.score),
      table(['Family', 'Detectors', 'Family cap', 'Group (independent source)', 'Group cap', 'Signals'],
        fams.map(f => [f.label, f.detectors.join(', '), f.scored ? f.cap : 'not scored', f.group, f.group_cap === null ? '—' : f.group_cap, lsInt(f.signals)])),
      muted('Tiers (config): medium ≥ ' + t.medium + '; high ≥ ' + t.high + ' with ≥ ' + t.min_sources_high + ' independent sources; critical ≥ ' + t.critical + ' with ≥ ' + t.min_sources_critical + '. Select a transaction below for its exact breakdown.'),
    ]);
  }

  /* ---------------- findings summary (insights) ---------------- */
  function renderInsights(s){
    setText('insights-total-fraud', s.amounts.high_or_critical === null ? '—' : lsAmount(s.amounts.high_or_critical, s.amounts.currency));
    setText('insights-vendors', lsInt(s.entities.with_high_or_critical_transactions));
    const lead = s.method.families.filter(f => f.scored && f.transactions_led > 0).sort((a, b) => b.transactions_led - a.transactions_led)[0];
    setText('insights-pattern', lead ? lead.label + ' — leads ' + lsInt(lead.transactions_led) + ' rows' : 'No detector raised a signal');
    const doc = $('print-doc');
    const parts = [el('h1', null, 'Findings Summary'), el('p', { style: 'text-align:center;font-family:var(--mono);font-size:0.7rem;margin-bottom:24px;color:var(--txt3)' }, 'RENDERED FROM THE BACKEND ANALYSIS · result ' + s.result_sha256.slice(0, 16))];
    parts.push(el('h2', null, 'Scope'), el('p', null, lsInt(s.counts.transactions) + ' transactions; ' + lsInt(s.counts.scored) + ' scored, ' + lsInt(s.counts.insufficient_data) + ' not assessed. Data readiness: ' + (s.readiness.status === 'not_run' ? 'not run' : Number(s.readiness.score).toFixed(1) + '% (' + s.readiness.status.replace(/_/g, ' ') + ')') + '.'));
    parts.push(el('h2', null, 'Result'), el('p', null, 'Critical ' + lsInt(s.counts.tiers.critical) + ', high ' + lsInt(s.counts.tiers.high) + ', medium ' + lsInt(s.counts.tiers.medium) + ', low ' + lsInt(s.counts.tiers.low) + '. ' + s.volume_guard.message + '.'));
    parts.push(el('h2', null, 'Review queue (top 10)'), el('ul', { class: 'ls-list' }, s.top_findings.length ? s.top_findings.map(f => el('li', null,
      f.txn_id + ' — ' + f.tier + ' ' + lsScore(f.score) + ' — ' + lsAmount(f.amount, f.currency) + ' — ' + (f.payee || '—') + ' — ' + f.key_findings.map(k => k.label + ' (' + k.contribution.toFixed(2) + ')').join('; ')))
      : el('li', null, 'Empty: no transaction reached the medium tier.')));
    parts.push(el('h2', null, 'Dataset-level findings'), el('ul', { class: 'ls-list' }, s.dataset_findings.length ? s.dataset_findings.map(f => el('li', null, f.detector + ': ' + f.explanation)) : el('li', null, 'None.')));
    parts.push(el('h2', null, 'Detectors not available on this data'), el('ul', { class: 'ls-list' }, s.detectors_unavailable.length ? s.detectors_unavailable.map(d => el('li', null, d.detector + ' — ' + d.reason)) : el('li', null, 'None.')));
    parts.push(el('h2', null, 'Limitations'), el('p', null, s.method.score));
    setChildren(doc, parts);
    $('memo-copy-btn').disabled = false;
  }

  /* ---------------- transaction table (server-side paging, filter, search) ---------------- */
  async function loadTable(){
    const tbody = $('tx-body');
    let page;
    try {
      page = await LS_API.get(base() + '/transactions' + LS_API.qs({ page: LS_STATE.page, size: LS_STATE.size, tier: LS_STATE.tier, q: LS_STATE.q, sort: LS_STATE.sort }));
    } catch(e){
      setChildren(tbody, el('tr', null, el('td', { colspan: '7' }, 'Could not load transactions: ' + e.message)));
      return;
    }
    const frag = document.createDocumentFragment();
    page.items.forEach((r, i) => {
      const main = el('tr', { class: 'main-row', 'data-tier': r.status === 'scored' ? r.tier : 'insufficient_data', 'data-txn': r.txn_id }, [
        el('td', null, el('div', { class: 'exp-icon' }, '▶')),
        el('td', { class: 'tx-id' }, r.txn_id),
        el('td', { style: 'font-family:var(--mono);color:var(--txt2)' }, r.posted_at || '—'),
        el('td', null, (r.payer || '—') + ' → ' + (r.payee || '—')),
        el('td', { style: 'font-family:var(--mono);font-weight:500' }, lsAmount(r.amount, r.currency)),
        el('td', null, lsTierBadge(r.status === 'scored' ? r.tier : 'insufficient_data', r.score)),
        el('td', { style: 'font-size:0.75rem;color:var(--txt2)' }, r.key_findings.length ? r.key_findings.map(k => k.label + ' (' + k.contribution.toFixed(2) + ')').join('; ') : '—'),
      ]);
      const det = el('tr', { class: 'detail-row', id: 'det-' + i }, el('td', { colspan: '7', style: 'padding:0;border:none' }, el('div', { class: 'detail-content ls-detail' }, muted('Loading explanation…'))));
      main.addEventListener('click', async () => {
        const open = det.style.display === 'table-row';
        det.style.display = open ? 'none' : 'table-row';
        main.querySelector('.exp-icon').style.transform = open ? 'rotate(0)' : 'rotate(90deg)';
        window._selectedTxn = r.txn_id;
        if(!open && !det.dataset.loaded){
          try {
            const d = await LS_API.get(base() + '/transactions/' + encodeURIComponent(r.txn_id));
            setChildren(det.querySelector('.detail-content'), renderExplanation(d));
            det.dataset.loaded = '1';
          } catch(e){ setChildren(det.querySelector('.detail-content'), na('Could not load: ' + e.message)); }
        }
      });
      frag.appendChild(main); frag.appendChild(det);
    });
    setChildren(tbody, []);
    tbody.appendChild(frag);
    if(!page.items.length) tbody.appendChild(el('tr', null, el('td', { colspan: '7', style: 'text-align:center;padding:16px;color:var(--txt3)' }, 'No transactions match.')));
    const pill = $('search-count-pill');
    pill.style.display = LS_STATE.q || LS_STATE.tier ? 'inline-flex' : 'none';
    pill.textContent = lsInt(page.total) + ' match' + (page.total === 1 ? '' : 'es');
    setChildren($('tx-pager'), [
      page.page > 1 ? button('◀ Prev', () => { LS_STATE.page--; loadTable(); }) : null,
      el('span', { class: 'di-muted', id: 'tx-page-label' }, 'Page ' + page.page + ' of ' + page.pages + ' · ' + lsInt(page.total) + ' transactions · sorted by ' + page.sort.replace('_', ' ')),
      page.page < page.pages ? button('Next ▶', () => { LS_STATE.page++; loadTable(); }) : null,
    ].filter(Boolean));
  }

  /* ---------------- explanation object (rendered verbatim) ---------------- */
  function renderExplanation(d){
    const ex = d.explanation, t = d.transaction;
    const parts = [el('div', { class: 'ls-headline' }, ex.headline),
      kv({ 'Transaction': t.txn_id, 'Source row': t.source_row, 'Posted': t.posted_at, 'Amount': lsAmount(t.amount, t.currency),
           'Payer → payee': (t.payer || '—') + ' → ' + (t.payee || '—'), 'Reference': t.reference, 'Review priority': t.review_priority.replace(/_/g, ' ') + (t.review_rank ? ' (#' + t.review_rank + ')' : ''),
           'Tier reason': ex.tier_reason, 'Counterparty': d.entity ? d.entity.name + ' (' + d.entity.tier + ' ' + lsScore(d.entity.score) + ')' : '—' })];
    if(ex.contributors.length){
      parts.push(el('div', { class: 'detail-h' }, 'Contributors (exact share of the score; ' + ex.contributors_total + ' in total)'));
      ex.contributors.forEach(c => parts.push(el('div', { class: 'ls-contrib' }, [
        el('div', { class: 'ls-contrib-h' }, [el('span', null, c.detector_label + ' · ' + c.family_label + (c.family_cap_applied ? ' (family capped)' : '')),
                                             el('span', { class: 'ls-contrib-pts' }, '+' + c.contribution.toFixed(2) + ' pts (' + lsShare(c.share_of_score) + ')')]),
        el('div', { style: 'font-size:0.82rem;color:var(--txt2);line-height:1.5' }, c.explanation),
        kv(c.evidence),
        c.affected_transactions.length > 1 ? muted('Rows cited: ' + c.affected_transactions.join(', ')) : null,
      ])));
    }
    if(ex.data_quality_flags.length){
      parts.push(el('div', { class: 'detail-h' }, 'Data-quality flags (shown, never scored)'));
      ex.data_quality_flags.forEach(f => parts.push(el('div', { class: 'ls-contrib' }, [el('div', null, f.detector_label), muted(f.explanation)])));
    }
    parts.push(el('div', { class: 'detail-h' }, 'Limitations'), el('ul', { class: 'ls-list' }, ex.limitations.map(l => el('li', null, l))));
    // P15: open an investigation from this automated finding (the score itself is never changed)
    if(window.LS_INV && LS_STATE.datasetId) parts.push(window.LS_INV.button({ link_type: 'risk_transaction', dataset_id: LS_STATE.datasetId, ref_id: t.txn_id }, 'P9 risk row ' + t.txn_id));
    return parts;
  }
  window.lsRenderExplanation = renderExplanation;

  /* ---------------- events ---------------- */
  $('run-btn').addEventListener('click', analyse);
  $('upload-zone').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', e => { const f = e.target.files && e.target.files[0]; if(f) importFile(f); e.target.value = ''; });
  document.querySelectorAll('#risk-filters .t-filter').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#risk-filters .t-filter').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    LS_STATE.tier = b.dataset.tier; LS_STATE.page = 1;
    if(LS_STATE.datasetId) loadTable();
  }));
  $('tx-sort').addEventListener('change', e => { LS_STATE.sort = e.target.value; LS_STATE.page = 1; if(LS_STATE.datasetId) loadTable(); });
  let searchTimer = null;
  $('search-in').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { LS_STATE.q = e.target.value.trim(); LS_STATE.page = 1; if(LS_STATE.datasetId) loadTable(); }, 250);
  });
  $('memo-copy-btn').addEventListener('click', () => {
    const text = $('print-doc').innerText;
    const done = ok => setText('memo-copy-btn', ok ? '✓ Copied' : 'Copy failed — select the text manually');
    if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
    else done(false);
    setTimeout(() => setText('memo-copy-btn', '⧉ Copy Summary Text'), 2500);
  });

  loadDatasets();
})();

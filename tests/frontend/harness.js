#!/usr/bin/env node
/* ============================================================
   LedgerSpy frontend test harness — Node.js, no dependencies.
   Usage (from repo root):  node tests/frontend/harness.js
   P9: the page renders API results; it computes no risk. This harness
   loads the real frontend scripts into a sandbox with a minimal DOM stub
   and checks, without a browser:
     A1  no fabricated literals, external URLs, alert() or randomness
     A2  no label columns and no scoring engine in the frontend
     A3  escaping: chatbot answers built from malicious API values stay inert
     A5  display helpers pass backend values through exactly
     A6  transaction IDs named in a question are looked up exactly
     A8  without the local service nothing is shown as computed
   Browser-level checks live in tests/frontend/browser_check.py.
   ============================================================ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const FE = path.join(ROOT, 'frontend');

let passed = 0, failed = 0;
const results = [];
async function check(id, name, fn){
  try {
    const detail = await fn();
    passed++; results.push({ id, name, ok: true, detail });
    console.log(`  PASS  [${id}] ${name}${detail ? ' — ' + detail : ''}`);
  } catch(e){
    failed++; results.push({ id, name, ok: false, detail: e.message });
    console.log(`  FAIL  [${id}] ${name} — ${e.message}`);
  }
}
function assert(cond, msg){ if(!cond) throw new Error(msg); }

/* ---------- sandbox ---------- */
function makeSandbox(source){
  const document = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => { throw new Error('DOM not available in harness'); },
  };
  const window = { addEventListener: () => {}, LS_CHAT_SOURCE: source };
  const ctx = { window, document, console, setTimeout, clearTimeout, navigator: {}, encodeURIComponent };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  // Same load order as index.html (ui-core.js and integrity-panel.js need a real DOM and are not loaded here).
  for(const f of ['js/shared.js', 'js/api.js', 'js/risk-ui.js', 'js/chatbot-ui.js']){
    vm.runInContext(fs.readFileSync(path.join(FE, f), 'utf8'), ctx, { filename: f });
  }
  return ctx;
}
const run = (ctx, code) => vm.runInContext(code, ctx);

/* ---------- API fixture with hostile values (as an attacker-controlled ledger could produce) ---------- */
const BAD = '<img src=x onerror=alert(1)>';
const SUMMARY = {
  counts: { transactions: 12345, scored: 12000, insufficient_data: 345, tiers: { critical: 3, high: 41, medium: 210, low: 11746 },
            high_or_critical: 44, medium_or_higher: 254 },
  amounts: { currency: 'INR', high_or_critical: '1234567.89' },
  entities: { with_high_or_critical_transactions: 17 },
  volume_guard: { message: '44 high/critical rows are within the review budget of 240 ' + BAD },
  method: { score: 'Score text ' + BAD, tiers: { medium: 25, high: 50, critical: 75, min_sources_high: 2, min_sources_critical: 3 },
            families: [{ label: 'Duplicate ' + BAD, scored: true, cap: 0.6 }, { label: 'Data quality', scored: false, cap: 0 }] },
  top_findings: [{ txn_id: 'T-' + BAD }],
  detectors_unavailable: [{ detector: 'velocity', reason: 'no calendar time ' + BAD }],
};
const DETECTORS = { benford: { dataset: { first: { status: 'analyzed', n: 7271, mad: 0.0075, band: 'acceptable ' + BAD,
  chi2_p_value: 0, over_represented: [2, 3], note: 'A deviation is a lead ' + BAD } } } };
const ROW = { txn_id: 'LVC-JV-000812', tier: 'critical', score: 79.31, amount: '100000.00', currency: 'INR', payee: 'Halvorn ' + BAD };
function source(log){
  return {
    ready: true,
    summary: () => SUMMARY,
    detectors: () => DETECTORS,
    transactions: async params => { log.push(['transactions', params]); return { items: [ROW, { ...ROW, txn_id: BAD, score: 12 }] }; },
    detail: async id => {
      log.push(['detail', id]);
      if(id !== 'LVC-JV-000812') { const e = new Error('not found'); e.status = 404; throw e; }
      return { transaction: { txn_id: id }, explanation: { headline: 'Review-risk score 79.31 ' + BAD,
        contributors: [{ contribution: 34.89, detector_label: 'Large amount ' + BAD, explanation: 'Payment ' + BAD }],
        limitations: ['Not a probability ' + BAD] } };
    },
    selected: () => null,
  };
}

(async () => {
  console.log('LedgerSpy frontend harness (P9: API-rendered UI)\n');
  const feFiles = [];
  (function walk(d){ for(const f of fs.readdirSync(d)){ const p = path.join(d, f); fs.statSync(p).isDirectory() ? walk(p) : feFiles.push(p); } })(FE);
  const feText = feFiles.map(f => ({ f: path.relative(ROOT, f), t: fs.readFileSync(f, 'utf8') }));
  const strip = t => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"\\])\/\/.*$/gm, '$1');

  /* ---------- A1 ---------- */
  const FORBIDDEN = ['2,45,00,000', '₹2.45 Cr', '93.7%', '3.4x', 'zero-knowledge', '68% chance', 'Stationery L.T.D',
    'MG Road', 'PMLA', 'Cluster #4', 'Risk = 87%', '87% (SEVERE', 'Math.random', 'fonts.googleapis', 'fonts.gstatic',
    '+128%', '2.28&times;', 'FY 2024', 'runMockBSR', 'court-ready', '100% Offline', 'SHAP-style', 'calibrated v2',
    'Logistic regression weights', 'Insolvency Break', 'Co-Founder'];
  await check('A1', 'No fabricated literals, mock functions or external font references in frontend/', () => {
    const hits = [];
    feText.forEach(({ f, t }) => FORBIDDEN.forEach(w => { if(t.includes(w)) hits.push(`${f}: "${w}"`); }));
    assert(hits.length === 0, hits.join('; '));
    return `${FORBIDDEN.length} patterns × ${feText.length} files`;
  });
  await check('A1', 'No external http(s) URLs (except the SVG XML namespace)', () => {
    const hits = [];
    feText.forEach(({ f, t }) => (t.match(/https?:\/\/[^\s"'`)<>]+/g) || []).forEach(u => { if(!u.startsWith('http://www.w3.org/2000/svg')) hits.push(`${f}: ${u}`); }));
    assert(hits.length === 0, hits.join('; '));
  });
  await check('A1', 'No alert() dialogs used as fake actions', () => {
    const hits = feText.filter(({ f, t }) => f.endsWith('.js') && /\balert\(/.test(strip(t))).map(x => x.f);
    assert(hits.length === 0, hits.join(', '));
  });

  /* ---------- A2: no labels, no scoring engine ---------- */
  await check('A2', 'Frontend code never references label columns (isFraud …)', () => {
    const hits = feText.filter(({ f, t }) => f.endsWith('.js') && /isfraud|isflaggedfraud|labelColumns/i.test(strip(t))).map(x => x.f);
    assert(hits.length === 0, hits.join(', '));
  });
  await check('A2', 'No client-side scoring engine, weights, thresholds or score comparisons', () => {
    assert(!fs.existsSync(path.join(FE, 'js', 'ml-engine.js')), 'ml-engine.js still present');
    const bad = [];
    feText.filter(x => x.f.endsWith('.js')).forEach(({ f, t }) => {
      const s = strip(t);
      [/sigmoid/, /HEURISTIC_/, /riskScore/, /predictTransaction|engineerFeatures/, /\bthresholds\s*:/, /score\w*\s*(>=|<=|>|<)\s*\d/i, /Math\.exp\(/]
        .forEach(re => { if(re.test(s)) bad.push(f + ' ' + re); });
    });
    assert(bad.length === 0, bad.join('; '));
    return 'risk values come only from API responses';
  });

  /* ---------- A3: escaping ---------- */
  await check('A3', 'escapeHtml neutralises markup', () => {
    const ctx = makeSandbox(null);
    const out = run(ctx, `escapeHtml('<img src=x onerror="alert(1)">&\\'')`);
    assert(out === '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;', out);
  });
  await check('A3', 'Chatbot answers built from hostile API values contain no live markup, in every branch', async () => {
    const log = [];
    const ctx = makeSandbox(source(log));
    const qs = ['show summary', 'explain risk score', 'why is LVC-JV-000812 flagged?', 'why is ' + BAD + ' flagged',
                'high risk transactions', 'benford', 'unavailable detectors', 'hello'];
    const bad = [];
    for(const q of qs){
      const out = await run(ctx, `processQuery(${JSON.stringify(q)})`);
      if(/<(img|script|svg)[\s>]/i.test(out)) bad.push(q);
    }
    assert(bad.length === 0, 'unescaped markup in: ' + bad.join(' | '));
    return qs.length + ' queries';
  });
  await check('A3', 'innerHTML is only used for static markup or escaped chatbot text', () => {
    const offenders = [];
    feText.filter(x => x.f.endsWith('.js')).forEach(({ f, t }) => {
      t.split('\n').forEach((line, i) => {
        if(/\.innerHTML\s*=/.test(line) || /insertAdjacentHTML|outerHTML\s*=|document\.write/.test(line)){
          const allowed = f.endsWith('chatbot-ui.js') && (/msg-label">Assistant<\/div>' \+ html/.test(line) || /<span><\/span><span><\/span><span><\/span>/.test(line));
          if(!allowed) offenders.push(`${f}:${i + 1}`);
        }
      });
    });
    assert(offenders.length === 0, offenders.join(', '));
  });

  /* ---------- A5: values pass through exactly ---------- */
  await check('A5', 'Amounts are the backend decimal text, grouped, never re-rounded', () => {
    const ctx = makeSandbox(null);
    const cases = [['1234567.89', 'INR', '₹1,234,567.89'], ['-1000.05', 'INR', '₹-1,000.05'], ['999', 'JPY', '999 JPY'],
                   ['12345678901234567.01', 'INR', '₹12,345,678,901,234,567.01'], [null, 'INR', '—']];
    cases.forEach(([v, c, want]) => {
      const got = run(ctx, `lsAmount(${JSON.stringify(v)}, ${JSON.stringify(c)})`);
      assert(got === want, `${v} → ${got} (want ${want})`);
    });
    assert(run(ctx, 'lsScore(79.3)') === '79.30' && run(ctx, 'lsScore(null)') === '—', 'score display');
    assert(run(ctx, 'lsInt(12345)') === '12,345' && run(ctx, 'lsShare(0.0076, 2)') === '0.76%', 'int/share display');
  });
  await check('A5', 'Chatbot summary quotes the API numbers unchanged', async () => {
    const ctx = makeSandbox(source([]));
    const out = await run(ctx, `processQuery('show summary')`);
    ['12,345', '12,000', '345', '<code>3</code>', '<code>41</code>', '<code>210</code>', '11,746', '₹1,234,567.89', '17']
      .forEach(v => assert(out.includes(v), 'missing ' + v));
  });

  /* ---------- A6: exact ID lookup ---------- */
  await check('A6', 'A transaction ID in a question is looked up exactly; unknown IDs say "not found"', async () => {
    const log = [];
    const ctx = makeSandbox(source(log));
    const ok = await run(ctx, `processQuery('why is LVC-JV-000812 flagged?')`);
    assert(log.some(([k, v]) => k === 'detail' && v === 'LVC-JV-000812'), 'detail not requested for the exact ID');
    assert(ok.includes('LVC-JV-000812') && ok.includes('34.89'), ok.slice(0, 120));
    const missing = await run(ctx, `processQuery('why is LVC-JV-999999 flagged?')`);
    assert(/not found/i.test(missing), missing);
  });
  await check('A6', 'Review-queue answer asks the API for its ranking (no client-side sorting by score)', async () => {
    const log = [];
    const ctx = makeSandbox(source(log));
    await run(ctx, `processQuery('high risk transactions')`);
    const call = log.find(([k]) => k === 'transactions');
    assert(call && call[1].sort === 'priority', JSON.stringify(log));
  });

  /* ---------- A8: no backend → nothing computed ---------- */
  await check('A8', 'Without the session token the API client is unavailable and the assistant says so', async () => {
    const ctx = makeSandbox(null);
    assert(run(ctx, 'LS_API.available') === false, 'LS_API.available should be false without the token meta');
    const out = await run(ctx, `processQuery('show summary')`);
    assert(/No analysis loaded/.test(out) && /ledgerspy serve/.test(out), out);
    assert(run(ctx, `LS_API.qs({ q: 'a&b', page: 2, tier: '', x: null })`) === '?q=a%26b&page=2', 'query encoding');
  });

  console.log(`\n${passed} passed, ${failed} failed`);
  fs.mkdirSync(path.join(__dirname, 'out'), { recursive: true });
  fs.writeFileSync(path.join(__dirname, 'out', 'harness-results.json'), JSON.stringify({ passed, failed, results }, null, 2));
  process.exit(failed ? 1 : 0);
})();

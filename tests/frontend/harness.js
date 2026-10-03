#!/usr/bin/env node
/* ============================================================
   LedgerSpy P1 test harness — Node.js, no dependencies.
   Usage (from repo root):  node tests/frontend/harness.js
   Loads the real frontend scripts into a sandbox with a minimal
   DOM stub and checks the P1 acceptance criteria that do not need
   a browser (A1, A2, A3-logic, A5-logic, A6, A7-logic, A8).
   Browser-level checks live in tests/frontend/browser_check.py.
   ============================================================ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const FE = path.join(ROOT, 'frontend');
const PAYSIM = path.join(ROOT, 'data', 'raw', 'paysim_sample_10k.csv');
const MALICIOUS = path.join(__dirname, 'fixtures', 'malicious.csv');

let passed = 0, failed = 0;
const results = [];
function check(id, name, fn){
  try {
    const detail = fn();
    passed++; results.push({ id, name, ok: true, detail });
    console.log(`  PASS  [${id}] ${name}${detail ? ' — ' + detail : ''}`);
  } catch(e){
    failed++; results.push({ id, name, ok: false, detail: e.message });
    console.log(`  FAIL  [${id}] ${name} — ${e.message}`);
  }
}
function assert(cond, msg){ if(!cond) throw new Error(msg); }

/* ---------- sandbox ---------- */
function makeSandbox(){
  const document = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => { throw new Error('DOM not available in harness'); },
  };
  const window = { addEventListener: () => {} };
  const ctx = { window, document, console, setTimeout, clearTimeout, navigator: {} };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  // Same load order as index.html (ui-core.js is DOM/canvas-only and is not loaded here).
  for(const f of ['js/shared.js', 'js/data-pipeline.js', 'js/ml-engine.js', 'js/chatbot-ui.js']){
    vm.runInContext(fs.readFileSync(path.join(FE, f), 'utf8'), ctx, { filename: f });
  }
  return ctx;
}
function scan(csvText, name){
  const ctx = makeSandbox();
  vm.runInContext(`parseCsvText(${JSON.stringify(csvText)}, ${JSON.stringify(name)}); runMLOnData();`, ctx);
  return ctx;
}
const run = (ctx, code) => vm.runInContext(code, ctx);
const riskById = ctx => {
  const m = new Map();
  run(ctx, 'tableData').forEach(d => m.set(d.id, d._mlScore + '|' + d.r));
  return m;
};

/* ---------- CSV helpers ---------- */
function dropColumn(text, colName){
  const lines = text.replace(/^﻿/, '').trim().split(/\r?\n/);
  const hdr = lines[0].split(',');
  const i = hdr.indexOf(colName);
  if(i < 0) throw new Error('column not found: ' + colName);
  return lines.map(l => { const c = l.split(','); c.splice(i, 1); return c.join(','); }).join('\n');
}
function setColumn(text, colName, value){
  const lines = text.replace(/^﻿/, '').trim().split(/\r?\n/);
  const hdr = lines[0].split(',');
  const i = hdr.indexOf(colName);
  return [lines[0]].concat(lines.slice(1).map(l => { const c = l.split(','); c[i] = value; return c.join(','); })).join('\n');
}

console.log('LedgerSpy P1 harness\n');
const paysim = fs.readFileSync(PAYSIM, 'utf8');
const base = scan(paysim, 'paysim_sample_10k.csv');
const S = run(base, 'window._mlSummary');

/* ---------- A1: no fabricated literals / external URLs / randomness ---------- */
const feFiles = [];
(function walk(d){ for(const f of fs.readdirSync(d)){ const p = path.join(d, f); fs.statSync(p).isDirectory() ? walk(p) : feFiles.push(p); } })(FE);
const feText = feFiles.map(f => ({ f: path.relative(ROOT, f), t: fs.readFileSync(f, 'utf8') }));

const FORBIDDEN = ['2,45,00,000', '₹2.45 Cr', '93.7%', '3.4x', 'zero-knowledge', '68% chance', 'Stationery L.T.D',
  'MG Road', 'PMLA', 'Cluster #4', 'Risk = 87%', '87% (SEVERE', 'Math.random', 'fonts.googleapis', 'fonts.gstatic',
  '+128%', '2.28&times;', 'FY 2024', 'runMockBSR', 'court-ready', '100% Offline', 'SHAP-style', 'calibrated v2',
  'Logistic regression weights', 'Insolvency Break', 'Co-Founder'];
check('A1', 'No fabricated literals, mock functions or external font references in frontend/', () => {
  const hits = [];
  feText.forEach(({ f, t }) => FORBIDDEN.forEach(w => { if(t.includes(w)) hits.push(`${f}: "${w}"`); }));
  assert(hits.length === 0, hits.join('; '));
  return `${FORBIDDEN.length} patterns × ${feText.length} files`;
});
check('A1', 'No external http(s) URLs (except the SVG XML namespace)', () => {
  const hits = [];
  feText.forEach(({ f, t }) => (t.match(/https?:\/\/[^\s"'`)<>]+/g) || []).forEach(u => { if(!u.startsWith('http://www.w3.org/2000/svg')) hits.push(`${f}: ${u}`); }));
  assert(hits.length === 0, hits.join('; '));
});
check('A1', 'No alert() dialogs used as fake actions', () => {
  const hits = feText.filter(({ t }) => /\balert\(/.test(t)).map(x => x.f);
  assert(hits.length === 0, hits.join(', '));
});

/* ---------- A2: label leakage ---------- */
check('A2', 'Identical scores with vs without the isFraud column', () => {
  const noLabel = scan(dropColumn(paysim, 'isFraud'), 'no_label.csv');
  const a = riskById(base), b = riskById(noLabel);
  assert(a.size === b.size && a.size === 10127, `row counts ${a.size} vs ${b.size}`);
  let diff = 0; a.forEach((v, k) => { if(b.get(k) !== v) diff++; });
  assert(diff === 0, `${diff} rows differ`);
  return `${a.size} rows identical`;
});
check('A2', 'Identical scores when isFraud is set to all-1 and all-0', () => {
  const a = riskById(base);
  for(const v of ['1', '0']){
    const b = riskById(scan(setColumn(paysim, 'isFraud', v), 'set.csv'));
    let diff = 0; a.forEach((val, k) => { if(b.get(k) !== val) diff++; });
    assert(diff === 0, `isFraud=${v}: ${diff} rows differ`);
  }
  return 'both variants identical';
});
check('A2', 'Label columns are never mapped to engine fields', () => {
  const map = run(base, 'window._mlColMap');
  const hdr = run(base, '_csvRawHeaders');
  const used = Object.values(map).filter(i => i >= 0).map(i => hdr[i]);
  assert(!used.some(h => /fraud|label|target/i.test(h)), 'mapped: ' + used.join(','));
  return 'mapped columns: ' + used.join(', ');
});
check('A2', 'Engine source has no reference to isFraud', () => {
  const src = fs.readFileSync(path.join(FE, 'js/ml-engine.js'), 'utf8');
  assert(!/isFraud/i.test(src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')), 'isFraud referenced in code');
});

/* ---------- A3: XSS (logic level) ---------- */
check('A3', 'escapeHtml neutralises markup', () => {
  const out = run(base, `escapeHtml('<img src=x onerror="alert(1)">&\\'')`);
  assert(out === '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;', out);
});
check('A3', 'Chatbot output escapes malicious CSV values in every branch', () => {
  const mal = scan(fs.readFileSync(MALICIOUS, 'utf8'), 'malicious.csv');
  const qs = ['show summary', 'high risk transactions', 'top anomalies', 'suspicious', 'why is ROW-1 flagged?',
              'what action should I take?', 'benford', 'velocity', 'safe', 'why is <img src=x onerror=alert(1)> flagged'];
  const bad = [];
  qs.forEach(q => {
    const out = run(mal, `processQuery(${JSON.stringify(q)})`);
    // Escaped text such as '&lt;img … onerror=…' is harmless; only raw tags from the file are a failure.
    if(/<(img|script|svg|b)[\s>]/i.test(out)) bad.push(q);
  });
  assert(bad.length === 0, 'unescaped markup in: ' + bad.join(' | '));
  return qs.length + ' queries';
});
check('A3', 'innerHTML is only used for static markup or escaped chatbot text', () => {
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

/* ---------- A5 / A8: computed values ---------- */
check('A5', 'Tier counts are internally consistent', () => {
  assert(S.high + S.susp + S.safe === S.total && S.total === 10127, JSON.stringify(S));
  const rows = run(base, 'tableData');
  const c = { high: 0, suspicious: 0, safe: 0 };
  rows.forEach(d => c[d.r]++);
  assert(c.high === S.high && c.suspicious === S.susp && c.safe === S.safe, 'table tiers ≠ summary');
  return `high ${S.high}, suspicious ${S.susp}, below ${S.safe}`;
});
check('A5', 'Every score equals its clamped breakdown (exact explanation)', () => {
  const R = run(base, 'window._mlResults');
  let bad = 0;
  R.forEach(d => {
    const sum = d.mlResult.components.reduce((s, c) => s + c.pts, 0);
    if(Math.min(100, Math.max(0, Math.round(sum))) !== d.mlResult.riskScore) bad++;
  });
  assert(bad === 0, bad + ' rows mismatch');
  return R.length + ' rows';
});
check('A5', 'Benford chart data is computed from the file', () => {
  const b = run(base, 'window._mlBenford');
  assert(b.n > 0 && b.counts.reduce((s, c) => s + c, 0) === b.n, 'counts');
  assert(Math.abs(b.observedPct.reduce((s, p) => s + p, 0) - 100) < 1e-6, 'pct sum');
  return `n=${b.n}, digit 1 observed ${b.observedPct[0].toFixed(1)}%`;
});
check('A5', 'Thresholds shared by engine and chatbot (single source)', () => {
  const T = run(base, 'LS_CONFIG.thresholds');
  const txt = run(base, `processQuery('explain risk score')`);
  assert(txt.includes('≥' + T.high) && txt.includes(T.suspicious + '–' + (T.high - 1)), 'chatbot text does not reflect LS_CONFIG');
  const eng = fs.readFileSync(path.join(FE, 'js/ml-engine.js'), 'utf8');
  const bot = fs.readFileSync(path.join(FE, 'js/chatbot-ui.js'), 'utf8');
  assert(!/>=\s*(70|50|40)\b/.test(eng + bot) && !/≥\s*(70|50|40)\b/.test(bot), 'hard-coded threshold literal found');
  return `high ≥ ${T.high}, suspicious ≥ ${T.suspicious}`;
});
check('A8', 'Flag rate is reported and calibration warning raised when above budget', () => {
  const budget = run(base, 'LS_CONFIG.reviewBudgetPct');
  assert(typeof S.flaggedPct === 'number', 'no flaggedPct');
  assert(S.calibrationWarning === (S.flaggedPct > budget), 'warning flag inconsistent');
  const memoSrc = fs.readFileSync(path.join(FE, 'js/ml-engine.js'), 'utf8');
  assert(memoSrc.includes('NOT calibrated'), 'banner text missing');
  return `flag rate ${S.flaggedPct.toFixed(1)}% vs budget ${budget}% → warning ${S.calibrationWarning}`;
});

/* ---------- A6: IDs ---------- */
check('A6', 'No TX-??? IDs; IDs are unique ROW-n when the file has no ID column', () => {
  const ids = run(base, 'tableData').map(d => d.id);
  assert(!ids.some(i => i.includes('???')), 'TX-??? present');
  assert(new Set(ids).size === ids.length, 'duplicate IDs');
  assert(ids.every(i => /^ROW-\d+$/.test(i)), 'unexpected ID format');
  return `${ids.length} unique IDs`;
});
check('A6', 'ROW-n maps to data row n of the original file', () => {
  const rows = run(base, 'tableData');
  const raw = run(base, '_csvRawRows');
  const r123 = rows.find(d => d.id === 'ROW-123');
  assert(r123 && r123._raw === raw[122], 'ROW-123 does not point at data row 123');
  return 'ROW-123 → index column value ' + raw[122][0];
});
check('A6', 'IDs are stable across repeated scans (deterministic)', () => {
  const a = riskById(base), b = riskById(scan(paysim, 'again.csv'));
  let diff = 0; a.forEach((v, k) => { if(b.get(k) !== v) diff++; });
  assert(diff === 0, diff + ' differ');
});
check('A6', 'Chatbot "why is ROW-123 flagged?" resolves exactly ROW-123', () => {
  const out = run(base, `processQuery('why is ROW-123 flagged?')`);
  assert(out.includes('<strong>ROW-123</strong>'), out.slice(0, 120));
  const out2 = run(base, `processQuery('why is row 12 flagged?')`);
  assert(out2.includes('<strong>ROW-12</strong>'), 'row 12 not resolved exactly');
  const out3 = run(base, `processQuery('why is ROW-999999 flagged?')`);
  assert(/not found/i.test(out3), 'unknown ID should say not found');
});
check('A6', 'Uses the file\'s own ID column when present', () => {
  const mal = scan(fs.readFileSync(MALICIOUS, 'utf8'), 'malicious.csv');
  const ids = run(mal, 'tableData').map(d => d.id);
  assert(ids.includes('INV-0001'), ids.join(','));
});

/* ---------- A7: regression (logic level) ---------- */
check('A7', 'CSV export round-trips the original rows and neutralises formulas', () => {
  const mal = scan(fs.readFileSync(MALICIOUS, 'utf8'), 'malicious.csv');
  const csv = run(mal, 'ltBuildCsv()');
  assert(csv.split('\n').length === 1 + run(mal, '_csvRawRows').length, 'row count');
  assert(csv.includes("'=HYPERLINK") && !/(^|,)=HYPERLINK/m.test(csv), 'formula not neutralised');
  const pcsv = run(base, 'ltBuildCsv()');
  assert(pcsv.split('\n').length === 10128, 'paysim export rows');
  return 'export rows = header + data rows';
});
check('A7', 'Chatbot answers every suggestion chip', () => {
  ['Show summary', 'High risk transactions', 'Explain risk score', 'Top anomalies'].forEach(q => {
    const out = run(base, `processQuery(${JSON.stringify(q)})`);
    assert(out && !/No data loaded/.test(out), q);
  });
});
check('A7', 'Parser handles quoted commas, escaped quotes and BOM', () => {
  const ctx = makeSandbox();
  run(ctx, `parseCsvText('\\uFEFFid,vendor,amount\\n1,"Acme, Inc ""East""",100\\n', 'q.csv')`);
  const h = run(ctx, '_csvRawHeaders'), r = run(ctx, '_csvRawRows');
  assert(h[0] === 'id' && r[0][1] === 'Acme, Inc "East"' && r[0][2] === '100', JSON.stringify(r));
});

check('A5', 'Readiness score is computed over all rows and never shows 100% when issues exist', () => {
  const r = run(base, 'runDataIntegrityCheck(autoMapIdx())');
  assert(r.total === 10127, 'not all rows checked');
  assert(r.issues > 0 ? r.score < 100 : r.score === 100, JSON.stringify(r));
  return `score ${r.score}% with ${r.issues} issues`;
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.mkdirSync(path.join(__dirname, 'out'), { recursive: true });
fs.writeFileSync(path.join(__dirname, 'out', 'harness-results.json'), JSON.stringify({ passed, failed, summary: S && {
  total: S.total, high: S.high, susp: S.susp, safe: S.safe, avgRisk: S.avgRisk, flaggedPct: S.flaggedPct, calibrationWarning: S.calibrationWarning }, results }, null, 2));
process.exit(failed ? 1 : 0);

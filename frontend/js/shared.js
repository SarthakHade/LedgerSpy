/* ============================================================
   LedgerSpy — shared configuration + safe-rendering helpers
   ============================================================
   P1 (Truthful Baseline). This is the ONE place in the frontend
   where thresholds and label-column rules are defined. The scoring
   engine, table, chatbot and memo all read from here.

   NOTE: this is still a browser-side *heuristic preview*. From
   roadmap phase P9 onward these values move to the Python
   package's config/default.yaml and the frontend only renders
   results returned by the local API.
   ============================================================ */

const LS_CONFIG = Object.freeze({
  engineName: 'Heuristic preview (rule-based, not a trained model)',

  // Risk tiers on the 0–100 heuristic score. Used by the engine AND the chatbot text.
  thresholds: Object.freeze({ high: 70, suspicious: 40 }),

  // If more than this share of rows lands in high+suspicious, the preview is
  // considered uncalibrated and the UI says so (acceptance criterion A8).
  reviewBudgetPct: 5,

  // Ground-truth / label columns. They are NEVER used by mapping, features or scoring.
  // Compared after lower-casing and removing spaces, '_' and '-'.
  labelColumns: Object.freeze([
    'isfraud', 'fraud', 'isflaggedfraud', 'flaggedfraud', 'flagged',
    'label', 'target', 'groundtruth'
  ]),

  // Benford first-digit expectation (log10(1 + 1/d)), as percentages.
  benfordExpectedPct: Object.freeze([30.1, 17.6, 12.5, 9.7, 7.9, 6.7, 5.8, 5.1, 4.6]),
  // Heuristic: a digit counts as "over-represented" if observed − expected > this many
  // percentage points. Descriptive only — no statistical test until roadmap P7.
  benfordOverrepPP: 8,

  // Row identity when the file has no usable ID column.
  rowIdPrefix: 'ROW-',
  addedRowIdPrefix: 'NEW-',
});

/** Normalise a header for label-column comparison. */
function lsNormHeader(h){
  return String(h || '').toLowerCase().replace(/[\s_\-]/g, '');
}
/** True if a CSV header is a ground-truth/label column that must never be scored on. */
function lsIsLabelColumn(h){
  const n = lsNormHeader(h);
  return n !== '' && LS_CONFIG.labelColumns.includes(n);
}

/** Escape a value for safe insertion into HTML. Use for EVERY data-derived string. */
function escapeHtml(v){
  return String(v === undefined || v === null ? '' : v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Small DOM builder: el('div', {class:'x', style:'...'}, 'text' | Node | [children]). Text is always set via textContent. */
function el(tag, attrs, children){
  const node = document.createElement(tag);
  if(attrs){
    Object.entries(attrs).forEach(([k, v]) => {
      if(v === undefined || v === null) return;
      if(k === 'class') node.className = v;
      else if(k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    });
  }
  const add = c => {
    if(c === undefined || c === null || c === false) return;
    if(Array.isArray(c)) c.forEach(add);
    else if(typeof c === 'string' || typeof c === 'number') node.appendChild(document.createTextNode(String(c)));
    else node.appendChild(c);
  };
  add(children);
  return node;
}

/** Replace all children of a node. */
function setChildren(node, children){
  if(!node) return;
  while(node.firstChild) node.removeChild(node.firstChild);
  const arr = Array.isArray(children) ? children : [children];
  arr.forEach(c => { if(c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
}

/** Set textContent if the element exists. */
function setText(id, text){
  const n = (typeof document !== 'undefined') ? document.getElementById(id) : null;
  if(n) n.textContent = text;
}

/** Compact currency formatter (₹ display convention; the source file's currency is not verified). */
function formatAmtShort(v){
  v = Math.abs(v || 0);
  if(v >= 10000000) return '₹' + (v / 10000000).toFixed(2) + ' Cr';
  if(v >= 100000)   return '₹' + (v / 100000).toFixed(2) + ' L';
  if(v >= 1000)     return '₹' + (v / 1000).toFixed(1) + 'K';
  return '₹' + Math.round(v).toLocaleString();
}

/** Deterministic pseudo-random in [0,1) from an integer (used only for visual jitter). */
function lsHash01(i){
  let x = (i + 1) * 2654435761 >>> 0;
  x ^= x >>> 16; x = Math.imul(x, 2246822507) >>> 0;
  x ^= x >>> 13; x = Math.imul(x, 3266489909) >>> 0;
  x ^= x >>> 16;
  return (x >>> 0) / 4294967296;
}

/** Neutralise spreadsheet formula injection in exported CSV cells. */
function lsCsvSafeCell(v){
  const s = String(v === undefined || v === null ? '' : v);
  if(/^[=+@\t\r]/.test(s) || /^-[^0-9.]/.test(s)) return "'" + s;
  return s;
}

/* Node test-harness export (ignored in the browser). */
if(typeof module !== 'undefined' && module.exports){
  module.exports = { LS_CONFIG, lsIsLabelColumn, escapeHtml, formatAmtShort, lsHash01, lsCsvSafeCell };
}

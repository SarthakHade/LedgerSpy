/* ============================================================
   LedgerSpy — shared safe-rendering and display helpers
   ============================================================
   P9: the browser renders results returned by the local LedgerSpy
   API. It computes no risk, no tiers, no thresholds and no
   statistics. These helpers only format values for display and
   build DOM nodes safely (text is always set via textContent).
   ============================================================ */

/* Display names and badge styles for the tiers the BACKEND assigns. No thresholds live here. */
const LS_TIER = Object.freeze({
  critical:          Object.freeze({ cls: 'bg-crit',    txt: 'CRITICAL' }),
  high:              Object.freeze({ cls: 'bg-red',     txt: 'HIGH' }),
  medium:            Object.freeze({ cls: 'bg-amber',   txt: 'MEDIUM' }),
  low:               Object.freeze({ cls: 'bg-green',   txt: 'LOW' }),
  insufficient_data: Object.freeze({ cls: 'bg-neutral', txt: 'NOT ASSESSED' }),
});

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
      else if(k === 'style') node.style.cssText = v;  // CSSOM: allowed by the strict CSP (P20); attributes are not
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

/** Group the integer part of a decimal string with commas: '1234567.89' → '1,234,567.89'. Text in, text out. */
function lsGroupDigits(text){
  const s = String(text);
  const neg = s.startsWith('-');
  const [i, f] = (neg ? s.slice(1) : s).split('.');
  const grouped = i.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (neg ? '-' : '') + grouped + (f !== undefined ? '.' + f : '');
}

/** An amount exactly as the backend returned it (decimal text), with its currency. Null → '—'. */
function lsAmount(text, currency){
  if(text === null || text === undefined) return '—';
  const g = lsGroupDigits(text);
  if(currency === 'INR') return '₹' + g;
  return currency ? g + ' ' + currency : g;
}

/** Integer counts as returned by the API. */
function lsInt(n){
  return (n === null || n === undefined) ? '—' : Number(n).toLocaleString('en-US');
}

/** A share in [0, 1] returned by the API, as a percentage. */
function lsShare(x, digits){
  return (x === null || x === undefined) ? '—' : (x * 100).toFixed(digits === undefined ? 1 : digits) + '%';
}

/** A backend score (already rounded to hundredths) for display. */
function lsScore(x){
  return (x === null || x === undefined) ? '—' : Number(x).toFixed(2);
}

/** Badge node for a backend tier. */
function lsTierBadge(tier, score){
  const t = LS_TIER[tier] || LS_TIER.insufficient_data;
  return el('span', { class: 'badge ' + t.cls }, t.txt + (score !== null && score !== undefined ? ' · ' + lsScore(score) : ''));
}

/** Deterministic pseudo-random in [0,1) from an integer (used only for the decorative background). */
function lsHash01(i){
  let x = (i + 1) * 2654435761 >>> 0;
  x ^= x >>> 16; x = Math.imul(x, 2246822507) >>> 0;
  x ^= x >>> 13; x = Math.imul(x, 3266489909) >>> 0;
  x ^= x >>> 16;
  return (x >>> 0) / 4294967296;
}

/* Node test-harness export (ignored in the browser). */
if(typeof module !== 'undefined' && module.exports){
  module.exports = { LS_TIER, escapeHtml, lsGroupDigits, lsAmount, lsInt, lsShare, lsScore, lsHash01 };
}

/* P20 strict CSP (style-src 'self'): the page's own static styles are kept in data-style and applied once
   through the CSSOM, which the policy allows; style="" attributes in markup are blocked by it. Same
   precedence as before (inline), so scripts that later change an element's style behave as they did. */
if(typeof document !== 'undefined' && document.querySelectorAll){
  document.querySelectorAll('[data-style]').forEach(n => { n.style.cssText = n.getAttribute('data-style'); });
}

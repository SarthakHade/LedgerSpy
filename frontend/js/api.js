/* ============================================================
   LedgerSpy — client for the local LedgerSpy API (P9)
   ============================================================
   The page is served by `ledgerspy serve`, which injects a per-launch
   session token (<meta name="ledgerspy-session-token">). Every API call
   sends it as a Bearer token; the server also checks Host/Origin.
   Opened as a plain file there is no token: LS_API.available is false
   and the UI says the local service is required (nothing is computed
   in the browser instead).
   ============================================================ */
const LS_API = (function(){
  'use strict';
  const meta = (typeof document !== 'undefined' && document.querySelector)
    ? document.querySelector('meta[name="ledgerspy-session-token"]') : null;
  const token = meta ? meta.getAttribute('content') : null;

  async function call(method, path, body, raw){
    const init = { method, headers: { 'Authorization': 'Bearer ' + token }, credentials: 'same-origin' };
    if(raw !== undefined){
      init.headers['Content-Type'] = 'application/octet-stream';
      init.body = raw;
    } else if(body !== undefined){
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
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

  /** Build a query string from defined values only. */
  function qs(params){
    const parts = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v));
    return parts.length ? '?' + parts.join('&') : '';
  }

  return {
    available: !!token,
    qs,
    get: (path) => call('GET', path),
    post: (path, body) => call('POST', path, body),
    put: (path, body) => call('PUT', path, body),
    upload: (caseId, file) => call('POST', '/api/cases/' + caseId + '/files' + qs({ filename: file.name }), undefined, file),
  };
})();

if(typeof module !== 'undefined' && module.exports){ module.exports = { LS_API }; }

"""The demo's copy of the UI: the unchanged `frontend/` plus a persistent, non-dismissable banner.

The banner is plain markup and a separate stylesheet (no inline style or script), so the workspace app's
strict CSP check (`ledgerspy.api.security.inline_markup`) still passes. The offline `frontend/` is never
modified.
"""

import html
import os
import shutil
import stat
from pathlib import Path

from ledgerspy.paths import FRONTEND_DIR
from ledgerspy_demo.datadir import safe_remove_tree

BANNER_TEXT = (
    "Public demo — synthetic data only. Do not upload real financial, personal, or confidential data."
)
EPHEMERAL_NOTE = (
    "Work in this demo is not saved across service restarts and is deleted after at most {days} days."
)
PERSISTENT_NOTE = (
    "Work is saved on a best-effort basis and deleted after at most {days} days; recent changes can be lost."
)
# The visitor's save status, shown in the banner (same origin, so the strict CSP applies unchanged). The text
# comes from the server and is shown with textContent only.
STATUS_JS = """(function(){
  'use strict';
  var el = document.getElementById('ls-demo-save-status');
  if(!el){ return; }
  function show(text){ el.textContent = text; }
  function poll(){
    fetch('/demo/workspace/status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(s){
        if(!s){ show('Save status unavailable.'); return; }
        var when = s.last_saved_at ? ' Last saved: ' + new Date(s.last_saved_at).toLocaleString() + '.' : '';
        show(String(s.message || '') + when);
      })
      .catch(function(){ show('Save status unavailable.'); });
  }
  poll();
  setInterval(poll, 20000);
})();
"""
DEMO_CSS = """.ls-demo-banner{position:sticky;top:0;z-index:10000;display:block;margin:0;padding:8px 16px;
background:#7c2d12;color:#fff7ed;border-bottom:2px solid #fb923c;font:600 14px/1.4 system-ui,sans-serif;
text-align:center}
.ls-demo-banner small{display:block;font-weight:400;opacity:.9}
"""


def banner_html(retention_days: int, persistent: bool) -> str:
    note = (PERSISTENT_NOTE if persistent else EPHEMERAL_NOTE).format(days=retention_days)
    return (
        '<div class="ls-demo-banner" id="ls-demo-banner" role="alert">'
        f"{html.escape(BANNER_TEXT)}<small>{html.escape(note)}</small>"
        '<small id="ls-demo-save-status" aria-live="polite"></small></div>'
    )


def build_demo_frontend(dest: Path, retention_days: int, persistent: bool, src: Path = FRONTEND_DIR) -> Path:
    """Copy `src` to `dest` (replacing an earlier copy) and add the banner. Returns `dest`."""
    dest = Path(dest)
    if os.path.lexists(dest):
        safe_remove_tree(dest, dest.parent)  # never follows a link or junction in its place
    shutil.copytree(src, dest)
    # the demo owns its copy: copytree also copies the source's mode bits, which may be read-only (as in the
    # container image, where the application code is not writable by the service user)
    for folder, _dirs, files in os.walk(dest):
        os.chmod(folder, os.stat(folder).st_mode | stat.S_IRWXU)
        for name in files:
            path = os.path.join(folder, name)
            os.chmod(path, os.stat(path).st_mode | stat.S_IRUSR | stat.S_IWUSR)
    page = (dest / "index.html").read_text(encoding="utf-8")
    head_end = page.find("</head>")
    body = page.find("<body")
    body_end = page.find(">", body) if body >= 0 else -1
    if head_end < 0 or body_end < 0:
        raise ValueError("frontend/index.html has no </head> or <body>")
    page = (
        page[:head_end]
        + '<link rel="stylesheet" href="css/demo.css">\n<script src="js/demo-status.js" defer></script>\n'
        + page[head_end : body_end + 1]
        + "\n"
        + banner_html(retention_days, persistent)
        + page[body_end + 1 :]
    )
    (dest / "index.html").write_text(page, encoding="utf-8")
    (dest / "css" / "demo.css").write_text(DEMO_CSS, encoding="utf-8")
    (dest / "js" / "demo-status.js").write_text(STATUS_JS, encoding="utf-8")
    return dest

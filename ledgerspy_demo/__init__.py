"""The hosted public demo (Stage 2: isolated, ephemeral visitor workspaces; see docs/demo/public-demo.md).

This package is separate from `ledgerspy` on purpose:
- the offline application never imports it;
- the offline bundle's allow-list (`^ledgerspy/.+\\.py$` in packaging/build_bundle.py) does not ship it;
- nothing here changes the offline defaults, the loopback-only binding, the per-launch token or the CSP.

Modules:
- `limits`, `settings`: explicit opt-in with `LEDGERSPY_MODE=demo`; limits never looser than offline;
- `identity`: server-generated workspace IDs and signed cookies;
- `seed`: the synthetic seed workspace;
- `workspace`: the registry of per-workspace app instances;
- `frontdoor`: the public ASGI entry;
- `frontend`: the UI copy with the synthetic-data banner;
- `app`, `__main__`: assembly and entry point.

There is no persistence: work is lost when the service restarts.
"""

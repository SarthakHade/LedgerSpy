"""Command line: `ledgerspy serve` and `ledgerspy audit verify`."""

import argparse
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from ledgerspy import __version__
from ledgerspy.config import ConfigError, load_config


def _load(args: argparse.Namespace, overrides: dict[str, Any]) -> Any:
    if args.workspace:
        overrides["workspace"] = {"path": args.workspace}
    return load_config(args.config, overrides or None)


def cmd_serve(args: argparse.Namespace) -> int:
    from ledgerspy.api.app import build_server, create_app
    from ledgerspy.cases.audit import local_actor
    from ledgerspy.cases.db import db_path_for, init_db

    config = _load(args, {"server": {"port": args.port}} if args.port else {})
    db_path = db_path_for(config.settings)
    schema_version = init_db(db_path, local_actor())
    token = secrets.token_urlsafe(32)
    server = build_server(create_app(config, db_path, token), config)

    url = f"http://{config.settings.server.host}:{config.settings.server.port}/?token={token}"
    print(f"LedgerSpy {__version__} | schema v{schema_version} | database {db_path.resolve()}")
    print(f"Config {config.source} sha256 {config.sha256}")
    print(f"Open: {url}")
    print("Bound to loopback only. Press Ctrl+C to stop.", flush=True)

    if not args.no_browser:

        def open_when_ready() -> None:
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    server.run()
    return 0 if server.started else 1


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from ledgerspy.cases import audit
    from ledgerspy.cases.db import DatabaseError, connect, db_path_for

    db_path = Path(args.db) if args.db else db_path_for(_load(args, {}).settings)
    try:
        conn = connect(db_path, read_only=True)
    except (DatabaseError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        result = audit.verify(conn)
    except sqlite3.Error as exc:
        print(f"FAIL: {db_path}: cannot read audit log: {exc}")
        return 1
    finally:
        conn.close()
    if result.ok:
        print(f"OK: {db_path}: {result.message}; head {result.head_hash}")
        return 0
    print(f"FAIL: {db_path}: {result.message}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerspy", description="LedgerSpy offline forensic audit tool")
    parser.add_argument("--version", action="version", version=f"ledgerspy {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="config file (default: config/default.yaml)")
        p.add_argument("--workspace", help="workspace directory (overrides workspace.path)")

    serve = commands.add_parser("serve", help="serve the UI and API on 127.0.0.1")
    common(serve)
    serve.add_argument("--port", type=int, help="port (overrides server.port)")
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    serve.set_defaults(func=cmd_serve)

    audit_cmd = commands.add_parser("audit", help="audit-log tools")
    audit_sub = audit_cmd.add_subparsers(dest="audit_command", required=True)
    verify = audit_sub.add_parser("verify", help="verify the hash chain of the audit log")
    common(verify)
    verify.add_argument("--db", help="database file (default: from config workspace)")
    verify.set_defaults(func=cmd_audit_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

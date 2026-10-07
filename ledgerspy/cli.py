"""Command line: `ledgerspy serve`, `audit verify`, `evidence verify`, `synth` and `evaluate` (P10)."""

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


def cmd_evidence_verify(args: argparse.Namespace) -> int:
    from ledgerspy.cases import evidence
    from ledgerspy.cases.db import DatabaseError, connect, db_path_for

    db_path = Path(args.db) if args.db else db_path_for(_load(args, {}).settings)
    try:
        conn = connect(db_path, read_only=True)
    except (DatabaseError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        count = conn.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0]
        problems = evidence.verify_all(conn, db_path.parent)
    except sqlite3.Error as exc:
        print(f"FAIL: {db_path}: cannot read evidence records: {exc}")
        return 1
    finally:
        conn.close()
    if problems:
        for p in problems:
            print(f"FAIL: {p['file']}: {p['problem']} (recorded sha256 {p['sha256']})")
        return 1
    print(f"OK: {db_path}: {count} evidence files match their recorded SHA-256 and are read-only")
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    from datetime import date

    from ledgerspy.synth.generator import generate_to, params_from
    from ledgerspy.synth.output import GeneratorError

    cfg = load_config(args.config)
    try:
        params = params_from(
            cfg.settings.synth,
            seed=args.seed,
            start=date.fromisoformat(args.start) if args.start else None,
            years=args.years,
            scale=args.scale,
        )
        manifest = generate_to(params, Path(args.out), force=args.force)
    except (GeneratorError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    p = manifest["parameters"]
    print(f"SYNTHETIC dataset written to {Path(args.out).resolve()}")
    print(f"seed {p['seed']} | {p['start_date']} .. {p['end_date']} | scale {p['scale']}")
    for name, info in manifest["files"].items():
        rows = "" if info["rows"] is None else f"{info['rows']:>6} rows  "
        print(f"  {name:48} {rows}sha256 {info['sha256'][:16]}")
    print(f"scenarios: {manifest['scenario_count']} (ground truth in ground_truth/, never an input)")
    print(f"dataset sha256: {manifest['dataset_sha256']}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from ledgerspy.cases.db import connect, db_path_for
    from ledgerspy.errors import LedgerSpyError
    from ledgerspy.supervised import service

    cfg = _load(args, {})
    conn = connect(db_path_for(cfg.settings))
    try:
        report = service.evaluate(
            conn, cfg, args.dataset_id, args.labels, Path(args.ground_truth) if args.ground_truth else None
        )
    except LedgerSpyError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    path = Path(args.out) if args.out else service.write(report, Path(cfg.settings.workspace.path))
    if args.out:
        path.write_text(__import__("json").dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    k = cfg.settings.supervised.evaluation.top_k[-1]
    print(f"Supervised EXPERIMENT (not a fraud probability). Results: {path}")
    print(f"result sha256 {report['result_sha256']}")
    for r in report["comparison"]:
        pr = "—" if r["pr_auc"] is None else f"{r['pr_auc']:.3f}"
        print(
            f"  {r['split']:18} {r['model']:52} PR-AUC {pr:>6}  P@{k} {r[f'precision@{k}']}  "
            f"{r['calibration']}"
        )
    for w in report["warnings"]:
        print(f"  warning: {w}")
    return 0


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

    evidence_cmd = commands.add_parser("evidence", help="evidence tools")
    evidence_sub = evidence_cmd.add_subparsers(dest="evidence_command", required=True)
    ev_verify = evidence_sub.add_parser("verify", help="re-hash every evidence file against the database")
    common(ev_verify)
    ev_verify.add_argument("--db", help="database file (default: from config workspace)")
    ev_verify.set_defaults(func=cmd_evidence_verify)

    synth = commands.add_parser("synth", help="generate the deterministic synthetic company (SYNTHETIC data)")
    synth.add_argument("--out", required=True, help="output directory")
    synth.add_argument("--config", help="config file (default: config/default.yaml)")
    synth.add_argument("--seed", type=int, help="random seed (default: synth.seed)")
    synth.add_argument("--start", help="first day, YYYY-MM-DD (default: synth.start_date)")
    synth.add_argument("--years", type=int, help="number of years, 2-5 (default: synth.years)")
    synth.add_argument(
        "--scale", type=float, help="volume multiplier for normal entities (default: synth.scale)"
    )
    synth.add_argument("--force", action="store_true", help="replace an existing synthetic dataset in --out")
    synth.set_defaults(func=cmd_synth)

    ev = commands.add_parser("evaluate", help="P10: leakage-safe supervised experiment on a labelled dataset")
    common(ev)
    ev.add_argument("--dataset-id", required=True, help="canonicalised dataset ID (D-…)")
    ev.add_argument("--labels", required=True, choices=["paysim", "synthetic"], help="label source")
    ev.add_argument("--ground-truth", help="P5 ground-truth JSON (required for --labels synthetic)")
    ev.add_argument("--out", help="write the report here (default: <workspace>/experiments/)")
    ev.set_defaults(func=cmd_evaluate)
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

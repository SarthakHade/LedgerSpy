"""Command line: `ledgerspy serve`, `audit verify`, `evidence verify`, `synth`, `evaluate` (P10),
`model` (P11) and `entities` (P12)."""

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


def _print_failure(exc) -> None:
    print(f"ERROR: {exc.message}", file=sys.stderr)
    for c in exc.details.get("checks", []):
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['detail']}", file=sys.stderr)
    for pr in exc.details.get("problems", []):
        print(f"  - {pr}", file=sys.stderr)


def cmd_model(args: argparse.Namespace) -> int:
    """P11 model registry. Lifecycle changes are audit-logged; nothing becomes active without approval."""
    import json

    from ledgerspy.cases.audit import local_actor
    from ledgerspy.cases.db import connect, db_path_for, init_db
    from ledgerspy.errors import LedgerSpyError
    from ledgerspy.supervised import artifact, inference, registry, train

    cfg = _load(args, {})
    db_path = db_path_for(cfg.settings)
    actor = local_actor()
    init_db(db_path, actor)
    workspace = Path(cfg.settings.workspace.path)
    conn = connect(db_path)
    sub = args.model_command
    code = 0
    try:
        if sub == "train":
            doc = train.train(
                conn,
                cfg,
                args.dataset_id,
                args.labels,
                Path(args.ground_truth) if args.ground_truth else None,
                args.model,
                args.imbalance,
                args.split,
                args.ablation,
            )
            out = registry.register(conn, workspace, doc, actor, "trained with ledgerspy model train")
            ev = doc["evaluation"]["test"]
            tr = doc["training"]
            print("Supervised model artefact (EXPERIMENT: a ranking score, not a fraud probability)")
            print(f"  model {out['model_id']}  {doc['model']['type']}  status {out['status']}")
            print(f"  artefact sha256 {out['artifact_sha256']}  file models/{out['file']}")
            print(
                f"  training domain {tr['training_domain']} (production eligible: "
                f"{tr['production_eligible']})"
            )
            print(
                f"  test PR-AUC {ev['pr_auc']}  ROC-AUC {ev['roc_auc']}  calibration "
                f"{doc['calibration']['status']}"
            )
            for w in doc["evaluation"]["warnings"]:
                print(f"  warning: {w}")
            return 0
        if sub == "list" and not args.json:
            out = registry.list_models(conn, workspace)
            for m in out["models"]:
                flag = "ACTIVE " if m["active"] else ""
                print(
                    f"{flag}{m['model_id']}  {m['model_type']:20} {m['effective_status']:16} "
                    f"{m['training_domain']}  sha256 {m['artifact_sha256'][:16]}"
                )
            for f in out["unregistered_files"]:
                print(f"unregistered file (never used): {f}")
            print(f"active model: {out['active_model_id'] or 'none'}")
            return 0
        if sub == "list":
            out = registry.list_models(conn, workspace)
        elif sub == "register":
            out = registry.import_file(conn, workspace, Path(args.file), actor)
        elif sub == "inspect":
            out = registry.describe(conn, workspace, args.model_id, full=True)
        elif sub == "verify":
            if args.file:
                loaded = artifact.load_file(Path(args.file))
                out = {
                    "model_id": loaded.model_id,
                    "artifact_sha256": loaded.sha256,
                    "integrity": "verified",
                    "compatibility": artifact.compatibility(loaded.doc),
                }
                code = 0 if out["compatibility"]["compatible"] else 1
            else:
                out = registry.describe(conn, workspace, args.model_id)
                code = 1 if out["effective_status"] in ("integrity_failed", "incompatible") else 0
        elif sub in STATUS_COMMANDS:
            out = registry.set_status(
                conn, workspace, args.model_id, STATUS_COMMANDS[sub], actor, args.reason
            )
        elif sub == "activate":
            out = registry.activate(conn, workspace, args.model_id, actor, args.reason)
        elif sub == "deactivate":
            out = registry.deactivate(conn, workspace, actor, args.reason)
        else:  # infer
            res = inference.infer(conn, cfg, workspace, args.dataset_id, args.model_id, args.experimental)
            out = res.get("metadata", res)
            if args.out and "rows" in res:
                Path(args.out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    except LedgerSpyError as exc:
        _print_failure(exc)
        return 1
    finally:
        conn.close()
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return code


STATUS_COMMANDS = {"promote": "candidate", "approve": "approved", "retire": "retired", "demote": "experiment"}


def cmd_entities(args: argparse.Namespace) -> int:
    """P12 entity resolution: import master data, resolve a dataset, run the P5 validation."""
    import json

    from ledgerspy.cases.audit import local_actor
    from ledgerspy.cases.db import connect, db_path_for, init_db
    from ledgerspy.errors import LedgerSpyError
    from ledgerspy.paths import REPO_ROOT
    from ledgerspy.resolution import masters, service, validation

    cfg = _load(args, {})
    sub = args.entities_command
    if sub == "validate":
        ref = Path(args.reference) if args.reference else REPO_ROOT / "data" / "synthetic" / "reference"
        gt = (
            Path(args.ground_truth)
            if args.ground_truth
            else ref / "ground_truth" / "SYNTHETIC_ground_truth.json"
        )
        report = validation.run_validation(
            cfg.settings.features.resolution, ref, gt if gt.is_file() else None
        )
        text = json.dumps(report, indent=1, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        m = report["master_records"]
        print("Entity-resolution validation (P5 masters + variants + adversarial cases + P5 ledger)")
        print(
            f"  candidate recall {m['candidate_recall']}  auto-match precision "
            f"{m['auto_match_precision']}  "
            f"false merges {m['false_merges']}  false splits {m['false_splits']} "
            f"({m['false_splits_left_for_review']} left for review)"
        )
        print(
            f"  candidate pairs {report['candidates']['candidate_pairs']} of "
            f"{report['candidates']['possible_pairs']} (reduction "
            f"{report['candidates']['reduction_ratio']})"
        )
        if not args.out:
            print(text)
        return 0 if m["false_merges"] == 0 else 1
    db_path = db_path_for(cfg.settings)
    actor = local_actor()
    init_db(db_path, actor)
    conn = connect(db_path)
    try:
        if sub == "import-master":
            mapping = None
            if args.map:
                mapping = {}
                for item in args.map:
                    field, sep, column = item.partition("=")
                    if not sep:
                        print(f"ERROR: --map expects field=column, got {item!r}", file=sys.stderr)
                        return 2
                    mapping[field.strip()] = column.strip()
            out = masters.import_master(
                conn,
                cfg,
                Path(cfg.settings.workspace.path),
                args.case_id,
                Path(args.file),
                args.entity_type,
                actor,
                mapping=mapping,
                use_suggested=args.use_suggested,
            )
        elif sub == "masters":
            out = masters.list_master_files(conn, args.case_id)
        else:  # resolve
            rr = service.run(conn, cfg, args.dataset_id)
            out = service.summary(rr)
            if not args.json:
                st = out["stats"]
                print(
                    f"Entity resolution of {args.dataset_id} (resolution sha256 "
                    f"{out['resolution_sha256'][:16]}…)"
                )
                print(
                    f"  records {st['records_by_source']}  entities {st['entities']} "
                    f"({st['multi_record_entities']} with several records)  {st['entities_by_status']}"
                )
                print(
                    f"  candidate pairs {st['candidate_pairs']} of {st['possible_pairs']}  "
                    f"{st['pairs_by_status']}"
                )
                for k, v in out["unavailable"].items():
                    print(f"  unavailable: {k}: {v}")
                return 0
    except LedgerSpyError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, indent=1, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
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

    model = commands.add_parser("model", help="P11: supervised model artefacts, registry and inference")
    msub = model.add_subparsers(dest="model_command", required=True)
    m_train = msub.add_parser("train", help="train one model the P10 way and register it as an experiment")
    m_train.add_argument("--dataset-id", required=True, help="canonicalised dataset ID (D-…)")
    m_train.add_argument("--labels", required=True, choices=["paysim", "synthetic"], help="label source")
    m_train.add_argument("--ground-truth", help="P5 ground-truth JSON (required for --labels synthetic)")
    m_train.add_argument(
        "--model", default="gradient_boosting", choices=["logistic_regression", "gradient_boosting"]
    )
    m_train.add_argument("--imbalance", default="balanced", choices=["none", "balanced"])
    m_train.add_argument("--split", default="group", choices=["random_stratified", "chronological", "group"])
    m_train.add_argument("--ablation", help="a configured ablation, e.g. no_simulator_fields")
    m_reg = msub.add_parser("register", help="verify an artefact file and register a copy (as experiment)")
    m_reg.add_argument("--file", required=True)
    m_list = msub.add_parser("list", help="registered models with their effective status")
    m_list.add_argument("--json", action="store_true")
    m_inspect = msub.add_parser("inspect", help="metadata, verification and history of a registered model")
    m_inspect.add_argument("model_id")
    m_verify = msub.add_parser("verify", help="verify a registered model (ID) or an artefact file (--file)")
    m_verify.add_argument("model_id", nargs="?")
    m_verify.add_argument("--file")
    for name, text in (
        ("promote", "experiment → candidate (runs the gate)"),
        ("approve", "candidate → approved (approval gate)"),
        ("demote", "candidate → experiment"),
        ("retire", "retire a model (deactivates it if active)"),
        ("activate", "make an approved model the production model (re-runs the gate)"),
    ):
        mp = msub.add_parser(name, help=text)
        mp.add_argument("model_id")
        mp.add_argument("--reason", required=True)
    m_deact = msub.add_parser("deactivate", help="leave no active production model")
    m_deact.add_argument("--reason", required=True)
    m_infer = msub.add_parser("infer", help="score a dataset: the active model, or --model-id --experimental")
    m_infer.add_argument("--dataset-id", required=True)
    m_infer.add_argument("--model-id")
    m_infer.add_argument("--experimental", action="store_true", help="allow a model that is not active")
    m_infer.add_argument("--out", help="also write every score to this file")
    for mp in msub.choices.values():
        common(mp)
        mp.set_defaults(func=cmd_model)

    ents = commands.add_parser("entities", help="P12: master data import and entity resolution")
    esub = ents.add_subparsers(dest="entities_command", required=True)
    e_imp = esub.add_parser(
        "import-master", help="import a vendor / customer / employee / group-company master"
    )
    e_imp.add_argument("--case-id", required=True)
    e_imp.add_argument("--file", required=True)
    e_imp.add_argument(
        "--entity-type", required=True, choices=["vendor", "customer", "employee", "group_company"]
    )
    e_imp.add_argument(
        "--map",
        action="append",
        metavar="FIELD=COLUMN",
        help="explicit mapping (record_id, name, address, city, phone, email, bank_account, bank_code, "
        "tax_id); repeat",
    )
    e_imp.add_argument("--use-suggested", action="store_true", help="accept the suggested mapping explicitly")
    e_list = esub.add_parser("masters", help="master files imported for a case")
    e_list.add_argument("--case-id", required=True)
    e_res = esub.add_parser("resolve", help="resolve a dataset's counterparties (summary)")
    e_res.add_argument("--dataset-id", required=True)
    e_res.add_argument("--json", action="store_true")
    e_val = esub.add_parser("validate", help="P5 validation: false merges, false splits, recall, precision")
    e_val.add_argument("--reference", help="P5 reference directory (default: data/synthetic/reference)")
    e_val.add_argument("--ground-truth", help="P5 ground truth (evaluation only)")
    e_val.add_argument("--out", help="write the report here")
    for ep in esub.choices.values():
        common(ep)
        ep.set_defaults(func=cmd_entities)
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

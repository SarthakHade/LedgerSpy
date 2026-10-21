"""The seed workspace: the synthetic company, imported once through the normal services, then copied into
every new visitor workspace. Only files from `data/synthetic/reference/` (no ground truth) are used; a
visitor's workspace is never copied to another visitor.
"""

import json
import os
import secrets
from pathlib import Path
from typing import Any

from ledgerspy.cases import audit, evidence, store
from ledgerspy.cases.db import check_triggers, connect, init_db
from ledgerspy.config import EffectiveConfig
from ledgerspy.ingest import service as ingest
from ledgerspy.ingest.mapping import Mapping
from ledgerspy.paths import REPO_ROOT
from ledgerspy.quality.engine import run_integrity
from ledgerspy.resolution import masters
from ledgerspy_demo.datadir import safe_remove_tree

REFERENCE = REPO_ROOT / "data" / "synthetic" / "reference"
SEED_ACTOR = "demo:seed"
MARKER = "seed.json"
DATASETS = (("SYNTHETIC_ledger.csv", "ledger"), ("SYNTHETIC_bank_statement.csv", "bank_statement"))
MASTERS = (
    ("SYNTHETIC_vendor_master.csv", "vendor"),
    ("SYNTHETIC_employee_master.csv", "employee"),
    ("SYNTHETIC_customer_master.csv", "customer"),
)


class SeedError(RuntimeError):
    pass


def remove_tree(path: Path) -> None:
    """Delete a demo-owned folder that may contain read-only evidence, never following a link or junction
    (`datadir.safe_remove_tree`); a link in its place is refused."""
    path = Path(path)
    if os.path.lexists(path):
        safe_remove_tree(path, path.parent)


def verify_workspace(path: Path) -> str | None:
    """None if the workspace's audit chain, triggers and evidence verify; otherwise the reason."""
    db = path / "ledgerspy.db"
    if not db.is_file():
        return "the workspace database is missing"
    try:
        conn = connect(db, read_only=True)
    except Exception as exc:  # noqa: BLE001 — any failure is reported, never trusted
        return f"the workspace database cannot be opened ({type(exc).__name__})"
    try:
        chain = audit.verify(conn)
        if not chain.ok:
            return f"the audit log does not verify ({chain.message})"
        if not check_triggers(conn)["ok"]:
            return "the database triggers differ from the migrations"
        problems = evidence.verify_all(conn, path)
        if problems:
            return f"{len(problems)} evidence file(s) are missing or altered"
    except Exception as exc:  # noqa: BLE001
        return f"the workspace cannot be verified ({type(exc).__name__})"
    finally:
        conn.close()
    return None


def build_seed(seed_dir: Path, config_for: Any, reference: Path = REFERENCE) -> dict[str, Any]:
    """Build the seed in a temporary sibling and rename it into place; reuse a verified existing seed.

    `config_for(path)` returns the (demo-limited) EffectiveConfig for a workspace at `path`."""
    seed_dir = Path(seed_dir)
    marker = seed_dir / MARKER
    if marker.is_file():
        reason = verify_workspace(seed_dir)
        if reason is None:
            return json.loads(marker.read_text(encoding="utf-8"))
        raise SeedError(f"the existing seed does not verify: {reason}")
    tmp = seed_dir.with_name(f".{seed_dir.name}.building-{secrets.token_hex(4)}")
    tmp.mkdir(parents=True)
    try:
        cfg: EffectiveConfig = config_for(tmp)
        init_db(tmp / "ledgerspy.db", SEED_ACTOR)
        conn = connect(tmp / "ledgerspy.db")
        try:
            case_id = store.create_case(conn, name="Demo company (SYNTHETIC)", actor=SEED_ACTOR)["case_id"]
            datasets = []
            for fname, kind in DATASETS:
                staged = evidence.stage_file(reference / fname, tmp, cfg.settings.ingest.max_file_bytes)
                ds = ingest.ingest_upload(conn, cfg, tmp, case_id, staged, fname, kind, SEED_ACTOR)
                mapping = Mapping(**ds["mapping_suggestion"]["proposed_mapping"])
                ingest.confirm_mapping(conn, cfg, tmp, ds["dataset_id"], mapping, SEED_ACTOR)
                run_integrity(conn, cfg, ds["dataset_id"], SEED_ACTOR)
                datasets.append(ds["dataset_id"])
            for fname, etype in MASTERS:
                masters.import_master(
                    conn, cfg, tmp, case_id, reference / fname, etype, SEED_ACTOR, use_suggested=True
                )
        finally:
            conn.close()
        reason = verify_workspace(tmp)
        if reason is not None:
            raise SeedError(f"the new seed does not verify: {reason}")
        info = {
            "case_id": case_id,
            "datasets": datasets,
            "synthetic": True,
            "source": "data/synthetic/reference",
        }
        (tmp / MARKER).write_text(json.dumps(info, indent=2), encoding="utf-8")
        if seed_dir.exists():
            remove_tree(seed_dir)
        os.replace(tmp, seed_dir)
        return info
    except BaseException:
        remove_tree(tmp)
        raise

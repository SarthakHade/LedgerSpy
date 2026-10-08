"""P11 local model registry: registered artefacts, lifecycle status, the approval gate and the active model.

Storage. Artefacts live as files in `<workspace>/models/<model_id>.json` and stay independently verifiable.
The registry is three append-only tables in the workspace database (migration 4), and every change is also
written to the hash-chained audit log in the same transaction:
- `model_artifacts`: model ID ↔ artefact hash pinned at registration;
- `model_status_events`: lifecycle; the current status is the latest event;
- `model_activation_events`: the active production model is the latest event's model (NULL: none).

Lifecycle (recorded statuses): experiment → candidate → approved → retired; candidate may go back to
experiment; anything not retired may be retired; retired is final. `incompatible` and `integrity_failed` are
derived on every read (the file no longer verifies, or the running feature registry no longer matches) and
override the recorded status. Registration always starts at `experiment`; nothing becomes active by being
registered or by being present in the models directory, and only an approved model can be activated.

Gate. Promotion to candidate needs integrity against the pinned hash, exact feature-contract compatibility, a
model card, an evaluation record, a known calibration status and a declared training domain that the policy
makes production-eligible. Approval and activation re-run all of it and also need approval metadata (actor
and a reason). A PaySim- or synthetic-trained model therefore stays `experiment`: it can be inspected and run
explicitly as an experiment, but never becomes a candidate, approved or active.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import utc_now_iso
from ledgerspy.supervised import artifact

STATUSES = ("experiment", "candidate", "approved", "retired")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "experiment": ("candidate", "retired"),
    "candidate": ("experiment", "approved", "retired"),
    "approved": ("retired",),
    "retired": (),
}
MIN_REASON = 10


class ModelError(LedgerSpyError):
    pass


def models_dir(workspace: Path) -> Path:
    return Path(workspace) / "models"


def _row(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row:
    artifact.path_for(Path("."), model_id)  # validates the ID format
    row = conn.execute("SELECT * FROM model_artifacts WHERE model_id = ?", (model_id,)).fetchone()
    if row is None:
        raise ModelError("model_not_found", f"no registered model {model_id}", http_status=404)
    return row


def history(conn: sqlite3.Connection, model_id: str) -> list[dict[str, Any]]:
    return [
        {
            "status": r["status"],
            "actor": r["actor"],
            "ts": r["ts"],
            "reason": r["reason"],
            "details": json.loads(r["details"]),
        }
        for r in conn.execute(
            "SELECT status, actor, ts, reason, details FROM model_status_events WHERE model_id = ? ORDER "
            "BY seq",
            (model_id,),
        )
    ]


def recorded_status(conn: sqlite3.Connection, model_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM model_status_events WHERE model_id = ? ORDER BY seq DESC LIMIT 1", (model_id,)
    ).fetchone()
    return row[0]


def active_model_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT model_id FROM model_activation_events ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row else None


def load(conn: sqlite3.Connection, workspace: Path, model_id: str) -> artifact.LoadedArtifact:
    """The registered artefact, verified against the hash pinned at registration."""
    row = _row(conn, model_id)
    return artifact.load_file(artifact.path_for(models_dir(workspace), model_id), row["artifact_sha256"])


# ------------------------------------------------------------------------------------------------ register


def register(
    conn: sqlite3.Connection, workspace: Path, doc: dict[str, Any], actor: str, reason: str = "registered"
) -> dict[str, Any]:
    """Save and register a sealed artefact as `experiment` (idempotent for an identical artefact)."""
    loaded = artifact.load_bytes(artifact.to_bytes(doc))
    d = loaded.doc
    path = artifact.save(d, models_dir(workspace))
    existing = conn.execute(
        "SELECT artifact_sha256 FROM model_artifacts WHERE model_id = ?", (d["model_id"],)
    ).fetchone()
    if existing is not None:
        if existing[0] != loaded.sha256:
            raise ModelError(
                "artifact_conflict", f"{d['model_id']} is registered with another hash", http_status=409
            )
        return describe(conn, workspace, d["model_id"]) | {"already_registered": True, "file": path.name}
    now = utc_now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO model_artifacts (model_id, artifact_sha256, model_type, training_domain, "
            "label_source, "
            "registered_by, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                d["model_id"],
                loaded.sha256,
                d["model"]["type"],
                d["training"]["training_domain"],
                d["training"]["label_source"],
                actor,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO model_status_events (model_id, status, actor, ts, reason, details) VALUES (?, ?, "
            "?, ?, ?, ?)",
            (d["model_id"], d["initial_status"], actor, now, reason, "{}"),
        )
        audit.append(
            conn,
            actor,
            "model.registered",
            f"model:{d['model_id']}",
            {
                "artifact_sha256": loaded.sha256,
                "model_type": d["model"]["type"],
                "training_domain": d["training"]["training_domain"],
                "status": d["initial_status"],
            },
        )
    return describe(conn, workspace, d["model_id"]) | {"already_registered": False, "file": path.name}


def import_file(conn: sqlite3.Connection, workspace: Path, path: Path, actor: str) -> dict[str, Any]:
    """Verify an artefact file from elsewhere and register a copy (the original is never used)."""
    loaded = artifact.load_file(Path(path))
    return register(conn, workspace, loaded.doc, actor, f"imported from {Path(path).name}")


# ------------------------------------------------------------------------------------------------ read


def _metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Everything a reviewer needs, without the bulky parameters (their hash identifies them)."""
    m = doc["model"]
    p = m["parameters"]
    size = (
        {"coefficients": len(p["coef"])}
        if m["type"] == "logistic_regression"
        else {"trees": len(p["trees"]), "nodes": sum(len(t) for t in p["trees"])}
    )
    return {
        "model": {k: v for k, v in m.items() if k != "parameters"} | {"parameter_counts": size},
        "feature_names": doc["feature_names"],
        "feature_contract": doc["feature_contract"],
        "preprocessor": {
            "fitted_on": doc["preprocessor"]["fitted_on"],
            "design_columns": doc["preprocessor"]["design_columns"],
            "dropped": doc["preprocessor"]["dropped"],
            "preprocessor_sha256": doc["preprocessor"]["preprocessor_sha256"],
        },
        "threshold_policy": doc["threshold_policy"],
        "calibration": doc["calibration"],
        "training": doc["training"],
        "evaluation": doc["evaluation"],
        "model_card": doc["model_card"],
        "creation": doc["creation"],
        "integrity": doc["integrity"],
    }


def describe(
    conn: sqlite3.Connection, workspace: Path, model_id: str, *, full: bool = False
) -> dict[str, Any]:
    """Registry entry with its effective status (verifies the file and the contract on every call)."""
    row = _row(conn, model_id)
    status = recorded_status(conn, model_id)
    out: dict[str, Any] = {
        "model_id": model_id,
        "artifact_sha256": row["artifact_sha256"],
        "model_type": row["model_type"],
        "training_domain": row["training_domain"],
        "label_source": row["label_source"],
        "production_eligible": artifact.TRAINING_DOMAINS[row["label_source"]][1],
        "registered_by": row["registered_by"],
        "registered_at": row["registered_at"],
        "status": status,
        "active": active_model_id(conn) == model_id,
        "label": "experimental supervised model" if status != "approved" else "approved supervised model",
    }
    try:
        loaded = load(conn, workspace, model_id)
    except artifact.ArtifactError as exc:
        derived = "incompatible" if exc.code == "artifact_incompatible" else "integrity_failed"
        out |= {"effective_status": derived, "integrity": exc.to_dict(), "compatibility": None}
        return out
    compat = artifact.compatibility(loaded.doc)
    out["integrity"] = {"verified": True, "load_seconds": loaded.load_seconds}
    out["compatibility"] = compat
    out["effective_status"] = status if compat["compatible"] else "incompatible"
    out["calibration_status"] = loaded.doc["calibration"]["status"]
    out["score_type"] = "uncalibrated ranking score"
    if full:
        out["metadata"] = _metadata(loaded.doc)
        out["history"] = history(conn, model_id)
    return out


def list_models(conn: sqlite3.Connection, workspace: Path) -> dict[str, Any]:
    ids = [
        r[0] for r in conn.execute("SELECT model_id FROM model_artifacts ORDER BY registered_at, model_id")
    ]
    known = set(ids)
    d = models_dir(workspace)
    unregistered = sorted(p.name for p in (d.glob("*.json") if d.is_dir() else []) if p.stem not in known)
    return {
        "models": [describe(conn, workspace, i) for i in ids],
        "active_model_id": active_model_id(conn),
        "unregistered_files": unregistered,
        "note": "Files in the models directory are never used unless registered; only an approved, active "
        "model is used for production inference. Supervised scores never change P9 review risk.",
    }


def active_status(conn: sqlite3.Connection, workspace: Path) -> dict[str, Any]:
    mid = active_model_id(conn)
    if mid is None:
        return {
            "active": False,
            "model_id": None,
            "status": "no_active_model",
            "note": "No supervised model is active. LedgerSpy's forensic workflow and P9 review risk do not "
            "need one.",
        }
    d = describe(conn, workspace, mid)
    usable = d["effective_status"] == "approved"
    return {
        "active": True,
        "model_id": mid,
        "usable": usable,
        "status": d["effective_status"],
        "model": d,
        "note": "Production supervised inference uses this model."
        if usable
        else "The active model no longer verifies or is incompatible: supervised inference is "
        "unavailable; P9 is unaffected.",
    }


# ------------------------------------------------------------------------------------------------ lifecycle


def gate(
    conn: sqlite3.Connection, workspace: Path, model_id: str, actor: str, reason: str, for_status: str
) -> list[dict[str, Any]]:
    """The approval-gate checks (each {check, passed, detail})."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    try:
        loaded = load(conn, workspace, model_id)
    except artifact.ArtifactError as exc:
        add("integrity", False, exc.message)
        return checks
    doc = loaded.doc
    add("integrity", True, f"artefact sha256 {loaded.sha256} matches the registered hash")
    compat = artifact.compatibility(doc)
    add("feature_contract", compat["compatible"], "; ".join(compat["problems"]) or "exact contract match")
    card = doc["model_card"]
    add("model_card", bool(card["intended_use"] and card["limitations"]), card["name"])
    ev = doc["evaluation"]
    add(
        "evaluation_record",
        ev["test"].get("pr_auc") is not None and bool(ev["experiment_id"]),
        f"{ev['experiment_id']}: test PR-AUC {ev['test'].get('pr_auc')}",
    )
    add(
        "calibration_status_known",
        doc["calibration"]["status"] in ("not_attempted", "calibrated", "not_validated"),
        doc["calibration"]["status"],
    )
    tr = doc["training"]
    add("training_domain_declared", bool(tr["training_domain"]), tr["training_domain"])
    add(
        "training_domain_production_eligible",
        tr["production_eligible"],
        tr["domain_note"] if not tr["production_eligible"] else "eligible",
    )
    if for_status in ("approved", "active"):
        add(
            "approval_metadata",
            bool(actor.strip()) and len(reason.strip()) >= MIN_REASON,
            f"actor {actor!r}; reason of at least {MIN_REASON} characters required",
        )
    return checks


def _event(conn, model_id, status, actor, reason, details) -> None:
    conn.execute(
        "INSERT INTO model_status_events (model_id, status, actor, ts, reason, details) VALUES (?, ?, ?, "
        "?, ?, ?)",
        (model_id, status, actor, utc_now_iso(), reason, json.dumps(details, sort_keys=True)),
    )
    audit.append(
        conn,
        actor,
        "model.status_changed",
        f"model:{model_id}",
        {"status": status, "reason": reason} | details,
    )


def _activation(conn, model_id, actor, reason) -> None:
    conn.execute(
        "INSERT INTO model_activation_events (model_id, actor, ts, reason) VALUES (?, ?, ?, ?)",
        (model_id, actor, utc_now_iso(), reason),
    )
    audit.append(
        conn,
        actor,
        "model.activated" if model_id else "model.deactivated",
        f"model:{model_id or 'none'}",
        {"reason": reason},
    )


def set_status(
    conn: sqlite3.Connection, workspace: Path, model_id: str, to: str, actor: str, reason: str
) -> dict[str, Any]:
    """Move a model along its lifecycle. Promotion and approval run the gate; failures change nothing."""
    if to not in STATUSES:
        raise ModelError("invalid_status", f"unknown status {to!r} (one of {', '.join(STATUSES)})")
    _row(conn, model_id)
    current = recorded_status(conn, model_id)
    if to not in TRANSITIONS[current]:
        raise ModelError(
            "model_transition_refused",
            f"{model_id}: {current} → {to} is not an allowed transition",
            {"allowed": list(TRANSITIONS[current])},
            http_status=409,
        )
    details: dict[str, Any] = {"from": current}
    if to in ("candidate", "approved"):
        checks = gate(conn, workspace, model_id, actor, reason, to)
        failed = [c for c in checks if not c["passed"]]
        if failed:
            raise ModelError(
                "approval_refused" if to == "approved" else "promotion_refused",
                f"{model_id} cannot become {to}: "
                + "; ".join(f"{c['check']} ({c['detail']})" for c in failed),
                {"checks": checks},
                http_status=409,
            )
        details["gate"] = checks
        details["artifact_sha256"] = _row(conn, model_id)["artifact_sha256"]
        if to == "approved":
            details["approval"] = {"approved_by": actor, "reason": reason}
    with transaction(conn):
        _event(conn, model_id, to, actor, reason, details)
        if to == "retired" and active_model_id(conn) == model_id:
            _activation(conn, None, actor, f"{model_id} retired")
    return describe(conn, workspace, model_id)


def activate(
    conn: sqlite3.Connection, workspace: Path, model_id: str, actor: str, reason: str
) -> dict[str, Any]:
    """Make an approved model the production model (re-runs the gate against the file and registry now)."""
    _row(conn, model_id)
    status = recorded_status(conn, model_id)
    checks = gate(conn, workspace, model_id, actor, reason, "active")
    failed = [c for c in checks if not c["passed"]]
    if status != "approved" or failed:
        raise ModelError(
            "activation_refused",
            f"{model_id} cannot be activated: "
            + "; ".join(
                (
                    [f"status is {status}, only an approved model can be active"]
                    if status != "approved"
                    else []
                )
                + [f"{c['check']} ({c['detail']})" for c in failed]
            ),
            {"checks": checks, "status": status},
            http_status=409,
        )
    with transaction(conn):
        _activation(conn, model_id, actor, reason)
    return active_status(conn, workspace)


def deactivate(conn: sqlite3.Connection, workspace: Path, actor: str, reason: str) -> dict[str, Any]:
    if active_model_id(conn) is not None:
        with transaction(conn):
            _activation(conn, None, actor, reason)
    return active_status(conn, workspace)

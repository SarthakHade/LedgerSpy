"""Generated report artefacts (P16): versioned, never overwritten, hash-checked on every read.

A generation builds the content (read-only), renders it, writes each format to
`<workspace>/reports/<report_id>.<ext>` with exclusive creation (an existing file is never replaced), makes
the files read-only, and records them — with their SHA-256 — in the append-only `reports` register together
with an audit entry, in one transaction. Regenerating the same report creates a new version; earlier versions
stay. File names are derived from the generated report ID only: no caller-supplied text reaches a path.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from ledgerspy import storage_limits
from ledgerspy.canonical.ids import new_ulid
from ledgerspy.cases import audit
from ledgerspy.cases.db import transaction
from ledgerspy.config.loader import EffectiveConfig
from ledgerspy.errors import LedgerSpyError
from ledgerspy.hashing import sha256_hex, utc_now_iso
from ledgerspy.reports import build, model, render

REPORT_ID = r"^RP-[0-9A-HJKMNP-TV-Z]{26}$"
_FILE = re.compile(r"^RP-[0-9A-HJKMNP-TV-Z]{26}\.(json|html|md)$")


def _err(code: str, message: str, status: int = 422, **details: Any) -> LedgerSpyError:
    return LedgerSpyError(code, message, details or None, http_status=status)


def reports_dir(workspace: Path) -> Path:
    return Path(workspace) / "reports"


def subject_key(report_type: str, params: dict[str, Any]) -> str:
    if report_type == "bank_reconciliation":
        return str(params.get("run_id"))
    return ",".join(sorted(params.get("investigation_ids") or []))


def preview(
    conn, cfg: EffectiveConfig, workspace, report_type: str, params: dict[str, Any]
) -> dict[str, Any]:
    """The content a generation would store now (nothing is written)."""
    return {"stored": False, "content": build.build(conn, cfg, workspace, report_type, params)}


def _artifacts(content: dict[str, Any], env: dict[str, Any]) -> dict[str, bytes]:
    out = {
        "json": (
            json.dumps({"envelope": env, "content": content}, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        "html": render.to_html(content, env).encode("utf-8"),
        "md": render.to_markdown(content, env).encode("utf-8"),
    }
    for fmt, data in out.items():
        if len(data) > model.MAX_ARTIFACT_BYTES:
            raise _err(
                "report_too_large",
                f"the {fmt} rendering is {len(data)} bytes, over the {model.MAX_ARTIFACT_BYTES}-byte limit; "
                "report fewer investigations at a time",
                413,
            )
    return out


def _write_new(path: Path, data: bytes) -> None:
    with open(path, "xb") as f:  # exclusive: an existing artefact is never overwritten
        f.write(data)
    os.chmod(path, stat.S_IREAD)


def generate(
    conn, cfg: EffectiveConfig, workspace, report_type: str, params: dict[str, Any], actor: str
) -> dict[str, Any]:
    content = build.build(conn, cfg, workspace, report_type, params)
    key = subject_key(report_type, params)
    folder = reports_dir(workspace)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        with transaction(conn):
            prev = conn.execute(
                "SELECT report_id, version, content_sha256 FROM reports WHERE report_type = ? AND "
                "subject_key = ? ORDER BY version DESC LIMIT 1",
                (report_type, key),
            ).fetchone()
            report_id = "RP-" + new_ulid()
            env = {
                "report_id": report_id,
                "report_type": report_type,
                "version": 1 if prev is None else prev["version"] + 1,
                "status": "draft" if report_type == "audit_memo" else "generated",
                "subject_key": key,
                "case_id": content["provenance"]["case_id"],
                "params": params,
                "generated_at": utc_now_iso(),
                "generated_by": actor,
                "previous_report_id": None if prev is None else prev["report_id"],
                "same_content_as_previous": prev is not None
                and prev["content_sha256"] == content["content_sha256"],
                "content_sha256": content["content_sha256"],
                "schema_version": content["schema_version"],
                "format_version": content["format_version"],
                "config_sha256": content["config_sha256"],
            }
            files = _artifacts(content, env)
            # no-op unless a host limits file output (the hosted demo); refused before anything is written
            storage_limits.claim_output(workspace, sum(len(d) for d in files.values()), "report")
            recorded = {}
            for fmt, data in files.items():
                path = folder / f"{report_id}.{fmt}"
                _write_new(path, data)
                written.append(path)
                recorded[fmt] = {"name": path.name, "sha256": sha256_hex(data), "bytes": len(data)}
            conn.execute(
                "INSERT INTO reports (report_id, report_type, subject_key, case_id, version, status, params, "
                "provenance, content_sha256, artifacts, schema_version, format_version, config_sha256, "
                "previous_report_id, generated_by, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    report_id,
                    report_type,
                    key,
                    env["case_id"],
                    env["version"],
                    env["status"],
                    json.dumps(params, sort_keys=True),
                    json.dumps(content["provenance"], sort_keys=True),
                    content["content_sha256"],
                    json.dumps(recorded, sort_keys=True),
                    env["schema_version"],
                    env["format_version"],
                    env["config_sha256"],
                    env["previous_report_id"],
                    actor,
                    env["generated_at"],
                ),
            )
            audit.append(
                conn,
                actor,
                "report.generated",
                f"report:{report_id}",
                {
                    "report_type": report_type,
                    "subject_key": key,
                    "version": env["version"],
                    "content_sha256": content["content_sha256"],
                    "artifacts": {f: r["sha256"] for f, r in recorded.items()},
                },
            )
    except BaseException:
        for p in written:  # the register row was rolled back: remove the files it would have described
            try:
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
                p.unlink()
            except OSError:
                pass
        raise
    return get(conn, report_id)


def _row(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("params", "provenance", "artifacts"):
        d[k] = json.loads(d[k])
    return d


def get(conn, report_id: str) -> dict[str, Any]:
    if not re.match(REPORT_ID, report_id or ""):
        raise _err("invalid_report_id", "not a report ID")
    r = conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
    if r is None:
        raise _err("report_not_found", "report not found", 404)
    out = _row(r)
    out["versions"] = [
        dict(x)
        for x in conn.execute(
            "SELECT report_id, version, generated_at, content_sha256 FROM reports WHERE report_type = ? AND "
            "subject_key = ? ORDER BY version",
            (r["report_type"], r["subject_key"]),
        )
    ]
    return out


def list_page(conn, case_id: str | None, report_type: str | None, page: int, size: int) -> dict[str, Any]:
    where, args = [], []
    if case_id:
        where.append("case_id = ?")
        args.append(case_id)
    if report_type:
        where.append("report_type = ?")
        args.append(report_type)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM reports" + clause, args).fetchone()[0]  # noqa: S608 — fixed fragments
    rows = conn.execute(
        "SELECT * FROM reports" + clause + " ORDER BY generated_at DESC, report_id DESC LIMIT ? OFFSET ?",  # noqa: S608
        [*args, size, (page - 1) * size],
    ).fetchall()
    return {"page": page, "size": size, "total": total, "items": [_row(r) for r in rows]}


def _path(workspace, name: str) -> Path:
    if not _FILE.match(name):  # pragma: no cover — names are generated, the register is append-only
        raise _err("invalid_artifact", "unexpected artefact name", 500)
    folder = reports_dir(workspace).resolve()
    path = (folder / name).resolve()
    if path.parent != folder:  # pragma: no cover — defence in depth
        raise _err("invalid_artifact", "artefact outside the reports folder", 500)
    return path


def read_artifact(conn, workspace, report_id: str, fmt: str) -> tuple[dict[str, Any], bytes]:
    if fmt not in model.FORMATS:
        raise _err("invalid_format", "unknown format", allowed=sorted(model.FORMATS))
    rep = get(conn, report_id)
    meta = rep["artifacts"][fmt]
    path = _path(workspace, meta["name"])
    if not path.is_file():
        raise _err("report_missing", "the report file is missing from the workspace", 409)
    data = path.read_bytes()
    if sha256_hex(data) != meta["sha256"]:
        raise _err("report_tampered", "the report file no longer matches its recorded SHA-256", 409)
    return rep, data


def verify(conn, workspace, report_id: str) -> dict[str, Any]:
    rep = get(conn, report_id)
    states = {}
    for fmt in sorted(rep["artifacts"]):
        try:
            read_artifact(conn, workspace, report_id, fmt)
            states[fmt] = "verified"
        except LedgerSpyError as exc:
            states[fmt] = exc.code
    return {"report_id": report_id, "artifacts": states, "ok": all(v == "verified" for v in states.values())}


def content(conn, workspace, report_id: str) -> dict[str, Any]:
    rep, data = read_artifact(conn, workspace, report_id, "json")
    doc = json.loads(data)
    return {"report": rep, "envelope": doc["envelope"], "content": doc["content"]}


def download_name(rep: dict[str, Any], fmt: str) -> str:
    return f"ledgerspy-{rep['report_type']}-v{rep['version']}-{rep['report_id']}.{fmt}"

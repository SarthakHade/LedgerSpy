"""P24 release blocker: a refused COMMIT ("database is locked") left the transaction open and an evidence file
without its database record, after which every retry of that file was refused (`evidence_conflict`).

Found during P24: the documented master-data import, run while the server was reading (the UI's graph), failed
intermittently. The lock is reproduced here deterministically: a second connection holds a read transaction
while a writer with a short busy timeout tries to commit.
"""

import json
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from ledgerspy import cli
from ledgerspy.cases import audit, store
from ledgerspy.cases.db import BUSY_TIMEOUT_SECONDS, connect, db_path_for, init_db, transaction
from ledgerspy.config import load_config
from ledgerspy.errors import LedgerSpyError
from ledgerspy.resolution import masters

REPO = Path(__file__).resolve().parents[2]
VENDORS = REPO / "data" / "synthetic" / "reference" / "SYNTHETIC_vendor_master.csv"


@pytest.fixture
def ws(tmp_path):
    cfg = load_config(overrides={"workspace": {"path": str(tmp_path / "ws")}})
    db = db_path_for(cfg.settings)
    init_db(db, "t")
    c = connect(db)
    case = store.create_case(c, name="p24", actor="t")["case_id"]
    c.close()
    return {"cfg": cfg, "db": db, "root": tmp_path / "ws", "case": case}


def hold_read_lock(db: Path, seconds: float) -> threading.Thread:
    """A reader that keeps a SHARED lock, as a long server read does."""
    ready = threading.Event()

    def run():
        r = sqlite3.connect(db, isolation_level=None)
        r.execute("BEGIN")
        r.execute("SELECT * FROM audit_log").fetchall()
        ready.set()
        time.sleep(seconds)
        r.execute("COMMIT")
        r.close()

    t = threading.Thread(target=run)
    t.start()
    ready.wait(10)
    return t


def test_connections_wait_for_a_lock_longer_than_sqlites_default(ws):
    for conn in (connect(ws["db"]), connect(ws["db"], read_only=True)):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(BUSY_TIMEOUT_SECONDS * 1000) >= 30000
        conn.close()


def test_a_refused_commit_is_rolled_back(ws):
    w = connect(ws["db"], timeout=0.2)
    before = w.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    t = hold_read_lock(ws["db"], 1.5)
    with pytest.raises(sqlite3.OperationalError, match="locked"), transaction(w):
        audit.append(w, "t", "test.write", "x:1", {})
    assert not w.in_transaction  # before P24: still open, holding uncommitted writes
    assert w.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    t.join()
    w.close()
    fresh = connect(ws["db"])
    assert fresh.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    assert audit.verify(fresh).ok


def test_an_import_refused_by_a_busy_database_succeeds_when_retried(ws):
    c = connect(ws["db"], timeout=0.2)
    t = hold_read_lock(ws["db"], 1.5)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        masters.import_master(
            c, ws["cfg"], ws["root"], ws["case"], VENDORS, "vendor", "t", use_suggested=True
        )
    assert not c.in_transaction
    t.join()
    evidence = ws["root"] / "evidence"
    left = list(evidence.iterdir())
    assert len(left) == 1  # the moved file stays (the move is not part of the SQLite transaction) …
    assert c.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0] == 0  # … without its record
    out = masters.import_master(  # before P24: refused with evidence_conflict on every retry
        c, ws["cfg"], ws["root"], ws["case"], VENDORS, "vendor", "t", use_suggested=True
    )
    assert out["already_imported"] is False and out["record_count"] > 0
    rows = c.execute("SELECT stored_name, sha256 FROM evidence_files").fetchall()
    assert [r[0] for r in rows] == [left[0].name]
    assert not left[0].stat().st_mode & stat.S_IWRITE  # read-only, like any evidence
    stored = c.execute(
        "SELECT details FROM audit_log WHERE action = 'evidence.stored' ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0]
    assert json.loads(stored)["recovered_unregistered_file"] is True
    assert c.execute("SELECT COUNT(*) FROM master_files").fetchone()[0] == 1
    assert audit.verify(c).ok
    c.close()


def test_an_unregistered_file_with_other_bytes_is_still_refused(ws):
    import hashlib

    sha = hashlib.sha256(VENDORS.read_bytes()).hexdigest()
    evidence = ws["root"] / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{sha}.csv").write_bytes(b"not the same content")
    c = connect(ws["db"])
    with pytest.raises(LedgerSpyError) as e:
        masters.import_master(
            c, ws["cfg"], ws["root"], ws["case"], VENDORS, "vendor", "t", use_suggested=True
        )
    assert e.value.code == "evidence_conflict"
    assert c.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0] == 0
    c.close()


def test_the_cli_reports_a_busy_database_explicitly(monkeypatch, capsys):
    def busy(_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "cmd_doctor", busy)
    assert cli.main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "busy" in err and "rolled back" in err and "Traceback" not in err

    def other(_args):
        raise sqlite3.OperationalError("no such table: x")

    monkeypatch.setattr(cli, "cmd_doctor", other)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        cli.main(["doctor"])  # only a busy database is turned into a message; other errors are not hidden

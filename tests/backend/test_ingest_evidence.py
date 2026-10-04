"""P3: evidence preservation — exact SHA-256, read-only copies, immutable records, tamper detection."""

import hashlib
import os
import sqlite3
import stat

import pytest

from ledgerspy.cases import audit, evidence
from ledgerspy.cli import main
from ledgerspy.ingest.errors import IngestError
from tests.backend.conftest import FIXTURES, PAYSIM


def stored_path(ingest, ds):
    return ingest.workspace / "evidence" / ds["evidence"]["stored_name"]


# ---------- 19. SHA-256 correctness ----------


@pytest.mark.parametrize("source", [PAYSIM, FIXTURES / "ledger_bom_crlf.csv", FIXTURES / "cp1252.csv"])
def test_recorded_hash_equals_sha256_of_original_bytes(ingest, source):
    original = source.read_bytes()
    ds = ingest.upload(source)
    expected = hashlib.sha256(original).hexdigest()
    assert ds["evidence"]["sha256"] == expected
    row = ingest.conn.execute("SELECT sha256, size_bytes FROM evidence_files").fetchone()
    assert tuple(row) == (expected, len(original))
    stored = stored_path(ingest, ds)
    assert stored.read_bytes() == original  # byte-identical copy: BOM, CRLF and encoding untouched
    assert stored.name == expected + "." + ds["evidence"]["format"]
    assert source.read_bytes() == original  # the user's file is never modified


def test_filename_is_metadata_never_a_path(ingest):
    ds = ingest.upload((FIXTURES / "ledger_normal.csv").read_bytes(), "..\\..\\evil name.csv")
    assert ds["evidence"]["original_filename"] == "..\\..\\evil name.csv"
    assert stored_path(ingest, ds).parent == ingest.workspace / "evidence"


def test_same_file_is_stored_once(ingest):
    first = ingest.upload(FIXTURES / "ledger_normal.csv")
    second = ingest.upload(FIXTURES / "ledger_normal.csv")
    assert first["evidence_newly_stored"] is True and second["evidence_newly_stored"] is False
    assert first["dataset_id"] != second["dataset_id"]
    assert len(list((ingest.workspace / "evidence").iterdir())) == 1
    assert not list((ingest.workspace / "tmp").iterdir())


# ---------- 20. immutability / read-only ----------


def test_evidence_copy_is_read_only(ingest):
    path = stored_path(ingest, ingest.upload(FIXTURES / "ledger_normal.csv"))
    assert not os.access(path, os.W_OK)
    assert not path.stat().st_mode & stat.S_IWRITE
    with pytest.raises(PermissionError):
        path.open("r+b")


def test_evidence_records_cannot_be_updated_or_deleted(ingest):
    ingest.upload(FIXTURES / "ledger_normal.csv")
    for sql in ("UPDATE evidence_files SET size_bytes = 1", "DELETE FROM evidence_files"):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            ingest.conn.execute(sql)


def test_canonicalised_dataset_is_frozen(ingest):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    ingest.confirm(ds)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        ingest.conn.execute("UPDATE datasets SET mapping = '{}'")
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        ingest.conn.execute("DELETE FROM datasets")


def test_tampered_evidence_blocks_canonicalisation_and_fails_verification(ingest, capsys):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    db = ingest.workspace / "ledgerspy.db"
    assert main(["evidence", "verify", "--db", str(db)]) == 0
    path = stored_path(ingest, ds)
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)  # someone deliberately bypasses the read-only flag
    path.write_bytes(path.read_bytes().replace(b"12500.00", b"125.00"))
    with pytest.raises(IngestError) as e:
        ingest.confirm(ds)
    assert e.value.code == "evidence_tampered"
    capsys.readouterr()
    assert main(["evidence", "verify", "--db", str(db)]) == 1
    assert "hash mismatch" in capsys.readouterr().out


def test_writable_evidence_is_reported(ingest, capsys):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    os.chmod(stored_path(ingest, ds), stat.S_IREAD | stat.S_IWRITE)
    assert evidence.verify_all(ingest.conn, ingest.workspace)[0]["problem"] == "not read-only"


def test_every_ingestion_step_is_in_the_verified_audit_chain(ingest):
    ds = ingest.upload(FIXTURES / "ledger_normal.csv")
    ingest.confirm(ds)
    actions = [r[0] for r in ingest.conn.execute("SELECT action FROM audit_log ORDER BY seq")]
    assert actions[-3:] == ["evidence.stored", "dataset.created", "dataset.canonicalised"]
    assert audit.verify(ingest.conn).ok

import sqlite3

import pytest

from ledgerspy.cases import audit, store
from ledgerspy.cases.db import SCHEMA_VERSION, DatabaseError, connect, current_version, init_db, transaction
from ledgerspy.hashing import canonical_json, sha256_hex


def fresh(tmp_path, name="a.db"):
    path = tmp_path / name
    init_db(path, "tester")
    return path


def raw(path):
    """A plain connection, as a person editing the file with the sqlite3 shell would have."""
    return sqlite3.connect(path, isolation_level=None)


def add_cases(path, n=3):
    conn = connect(path)
    try:
        for i in range(n):
            store.create_case(conn, name=f"Case {i}", actor="tester")
    finally:
        conn.close()


def verify(path):
    conn = connect(path, read_only=True)
    try:
        return audit.verify(conn)
    finally:
        conn.close()


# ---------- schema ----------


def test_init_creates_versioned_schema_and_first_audit_entry(tmp_path):
    path = fresh(tmp_path)
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"schema_version", "audit_log", "cases"}
    assert current_version(conn) == SCHEMA_VERSION == 1
    first = conn.execute("SELECT seq, action, prev_hash FROM audit_log").fetchall()
    assert [tuple(r) for r in first] == [(1, "schema.migrated", audit.GENESIS_HASH)]
    conn.close()


def test_init_is_deterministic(tmp_path):
    def snapshot(path):
        conn = raw(path)
        try:
            schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            versions = conn.execute("SELECT * FROM schema_version").fetchall()
            return schema, versions
        finally:
            conn.close()

    assert snapshot(fresh(tmp_path, "a.db")) == snapshot(fresh(tmp_path, "b.db"))


def test_init_is_idempotent(tmp_path):
    path = fresh(tmp_path)
    assert init_db(path, "tester") == SCHEMA_VERSION
    assert verify(path).entries == 1  # no second migration entry


def test_newer_schema_is_refused(tmp_path):
    path = fresh(tmp_path)
    conn = raw(path)
    conn.execute("INSERT INTO schema_version VALUES (99, 'future')")
    conn.close()
    with pytest.raises(DatabaseError, match="newer"):
        init_db(path, "tester")


def test_read_only_connect_never_creates_a_database(tmp_path):
    with pytest.raises(DatabaseError):
        connect(tmp_path / "missing.db", read_only=True)
    assert not (tmp_path / "missing.db").exists()


# ---------- chain ----------


def test_chain_links_each_entry_to_the_previous_one(tmp_path):
    path = fresh(tmp_path)
    add_cases(path, 3)
    conn = connect(path)
    rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    conn.close()
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert [r["action"] for r in rows[1:]] == ["case.created"] * 3
    prev = audit.GENESIS_HASH
    for r in rows:
        entry = {k: r[k] for k in ("seq", "ts", "actor", "action", "object", "details")}
        assert r["prev_hash"] == prev
        assert r["hash"] == sha256_hex(prev.encode() + canonical_json(entry))
        prev = r["hash"]


def test_verify_passes_on_untouched_database(tmp_path):
    path = fresh(tmp_path)
    add_cases(path, 5)
    result = verify(path)
    assert result.ok and result.entries == 6
    assert result.head_hash is not None and len(result.head_hash) == 64


def test_append_requires_a_transaction(tmp_path):
    conn = connect(fresh(tmp_path))
    with pytest.raises(RuntimeError):
        audit.append(conn, "t", "x", "y", {})
    conn.close()


def test_failed_change_rolls_back_its_audit_entry(tmp_path):
    path = fresh(tmp_path)
    conn = connect(path)
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        audit.append(conn, "t", "case.created", "case:x", {})
        conn.execute(
            "INSERT INTO cases (case_id, name, status, created_by, created_at) "
            "VALUES ('c', 'n', 'bogus', 't', 'now')"
        )
    conn.close()
    assert verify(path).entries == 1


def test_update_and_delete_are_blocked_by_triggers(tmp_path):
    path = fresh(tmp_path)
    conn = raw(path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET actor = 'mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.close()
    assert verify(path).ok


# ---------- tamper detection (manual DB edits) ----------


def tamper(path, *statements):
    conn = raw(path)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("DROP TRIGGER audit_log_no_delete")
    for s in statements:
        conn.execute(s)
    # Restore the triggers exactly, so only the data edit is left to detect.
    for ddl in audit.DDL[1:]:
        conn.execute(ddl)
    conn.close()


@pytest.mark.parametrize(
    ("statement", "failed_seq"),
    [
        ("UPDATE audit_log SET details = replace(details, 'Case 1', 'Case X') WHERE seq = 3", 3),
        ("UPDATE audit_log SET actor = 'mallory' WHERE seq = 2", 2),
        ("UPDATE audit_log SET ts = '2020-01-01T00:00:00.000000Z' WHERE seq = 4", 4),
        ("UPDATE audit_log SET action = 'case.deleted' WHERE seq = 1", 1),
        ("DELETE FROM audit_log WHERE seq = 2", 3),  # gap
        ("UPDATE audit_log SET prev_hash = hash WHERE seq = 3", 3),
    ],
)
def test_manual_edit_is_detected(tmp_path, statement, failed_seq):
    path = fresh(tmp_path)
    add_cases(path, 3)
    tamper(path, statement)
    result = verify(path)
    assert not result.ok
    assert result.failed_seq == failed_seq


def test_rehashing_an_edited_entry_breaks_the_next_link(tmp_path):
    path = fresh(tmp_path)
    add_cases(path, 3)
    conn = raw(path)
    row = conn.execute(
        "SELECT seq, ts, actor, action, object, details, prev_hash FROM audit_log WHERE seq=2"
    ).fetchone()
    seq, ts, actor, action, obj, details, prev = row
    forged_details = details.replace("Case 0", "Forged")
    forged_hash = audit.entry_hash(prev, seq, ts, actor, action, obj, forged_details)
    conn.close()
    tamper(path, f"UPDATE audit_log SET details = '{forged_details}', hash = '{forged_hash}' WHERE seq = 2")
    result = verify(path)
    assert not result.ok and result.failed_seq == 3


def test_appended_forged_entry_is_detected(tmp_path):
    path = fresh(tmp_path)
    conn = raw(path)
    conn.execute("INSERT INTO audit_log VALUES (2, 'ts', 'm', 'case.created', 'case:x', '{}', 'bad', 'bad')")
    conn.close()
    assert not verify(path).ok


def test_dropped_trigger_is_detected(tmp_path):
    path = fresh(tmp_path)
    conn = raw(path)
    conn.execute("DROP TRIGGER audit_log_no_delete")
    conn.close()
    result = verify(path)
    assert not result.ok and "trigger" in result.message


def test_emptied_log_is_detected(tmp_path):
    path = fresh(tmp_path)
    conn = raw(path)
    conn.execute("DROP TRIGGER audit_log_no_delete")
    conn.execute("DELETE FROM audit_log")
    conn.execute(audit.DDL[2])
    conn.close()
    assert not verify(path).ok

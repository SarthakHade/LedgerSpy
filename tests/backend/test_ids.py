import hashlib
import random
import re

import pytest

from ledgerspy.canonical import Entity, EntityKind, new_case_id, new_entity_id, new_ulid, txn_id
from ledgerspy.canonical.ids import TXN_ID_HEX_LEN

FILE_A = hashlib.sha256(b"evidence file A").hexdigest()
FILE_B = hashlib.sha256(b"evidence file B").hexdigest()


def test_txn_id_matches_the_documented_construction():
    expected = "T-" + hashlib.sha256(f"{FILE_A}:42".encode()).hexdigest()[:TXN_ID_HEX_LEN]
    assert txn_id(FILE_A, 42) == expected
    assert re.fullmatch(r"T-[0-9a-f]{16}", expected)


def test_same_file_and_row_always_give_the_same_id():
    assert len({txn_id(FILE_A, 7) for _ in range(100)}) == 1


def test_id_does_not_depend_on_processing_order():
    rows = list(range(1, 5001))
    in_order = {r: txn_id(FILE_A, r) for r in rows}
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    assert {r: txn_id(FILE_A, r) for r in shuffled} == in_order


def test_different_row_or_file_gives_a_different_id():
    assert txn_id(FILE_A, 1) != txn_id(FILE_A, 2)
    assert txn_id(FILE_A, 1) != txn_id(FILE_B, 1)


def test_no_collisions_within_and_across_files():
    n = 200_000
    ids = {txn_id(FILE_A, r) for r in range(1, n + 1)} | {txn_id(FILE_B, r) for r in range(1, n + 1)}
    assert len(ids) == 2 * n


@pytest.mark.parametrize(
    ("sha", "row"),
    [
        (FILE_A.upper(), 1),  # not canonical lowercase hex
        (FILE_A[:-1], 1),  # wrong length → hash input could become ambiguous
        (FILE_A[:-1] + "g", 1),  # not hex
        (FILE_A + ":1", 1),  # separator smuggled in
        (FILE_A, 0),  # rows are 1-based
        (FILE_A, -3),
        (FILE_A, True),  # bool is not a row number
        (FILE_A, "1"),
        (FILE_A, 1.0),
    ],
)
def test_invalid_inputs_are_rejected(sha, row):
    with pytest.raises(ValueError):
        txn_id(sha, row)


def test_source_id_is_used_verbatim_when_present():
    assert txn_id(FILE_A, 3, source_id="  INV-0001 ") == "INV-0001"
    with pytest.raises(ValueError):
        txn_id(FILE_A, 3, source_id="   ")


def test_ulid_format_ordering_and_uniqueness():
    assert new_ulid(0).startswith("0000000000")
    assert new_ulid(1_700_000_000_000)[:10] < new_ulid(1_800_000_000_000)[:10]
    ids = {new_ulid() for _ in range(10_000)}
    assert len(ids) == 10_000
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", u) for u in ids)
    with pytest.raises(ValueError):
        new_ulid(1 << 48)


def test_entity_and_case_ids_fit_their_models():
    Entity(entity_id=new_entity_id(), kind=EntityKind.VENDOR, canonical_name="Acme Ltd")
    assert re.fullmatch(r"C-[0-9A-HJKMNP-TV-Z]{26}", new_case_id())

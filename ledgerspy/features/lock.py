"""The registry lock: the committed (version, definition_sha256) of every implemented feature.

A test compares the live registry with `registry_lock.json`. When a definition changes (formula, inputs,
metadata, missing-data or fallback behaviour), its hash changes and the test fails until the feature's version
is bumped and the lock regenerated:

    python -m ledgerspy.features.lock          # rewrite registry_lock.json from the current definitions
"""

import json
from pathlib import Path

import ledgerspy.features  # noqa: F401  (registers every feature)
from ledgerspy.features.spec import lock_entries

LOCK_PATH = Path(__file__).with_name("registry_lock.json")


def read_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def write_lock() -> None:
    text = json.dumps(lock_entries(), indent=1, sort_keys=True) + "\n"
    LOCK_PATH.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    write_lock()
    print(f"wrote {LOCK_PATH} ({len(read_lock())} features)")

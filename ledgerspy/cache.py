"""Per-database namespaces for the engines' in-memory result caches.

The engines cache results by content identity (dataset, data version, configuration, ...). The offline
application has one database per process. The hosted demo (`ledgerspy_demo`, never shipped offline) runs
several workspace databases in one process, so every result cache is partitioned by the database file it was
computed from: a lookup from one database can neither see nor evict another database's entries, and an ID that
exists only in another database is simply not found. With one database the behaviour is that of one LRU of the
same size, as before.
"""

import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_NAMESPACES = 64  # safety bound; the demo forgets a workspace's namespace when it closes the workspace


def _normalise(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


def namespace(conn: sqlite3.Connection) -> str:
    """The namespace of a connection: its main database file ("" for an in-memory database)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return _normalise(row[2] or "")
    return ""


def namespace_for_path(db_path: Path | str) -> str:
    return _normalise(str(Path(db_path).resolve()))


class NamespacedLRU:
    """An LRU per namespace. `size` is the per-namespace bound (an int, or a callable read on each insert)."""

    def __init__(self, size: int | Callable[[], int]) -> None:
        self._size = size
        self._data: OrderedDict[str, OrderedDict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        _REGISTRY.append(self)

    def get(self, ns: str, key: str) -> Any | None:
        with self._lock:
            part = self._data.get(ns)
            if part is None or key not in part:
                return None
            part.move_to_end(key)
            self._data.move_to_end(ns)
            return part[key]

    def put(self, ns: str, key: str, value: Any, size: int | None = None) -> None:
        bound = size if size is not None else (self._size() if callable(self._size) else self._size)
        with self._lock:
            part = self._data.setdefault(ns, OrderedDict())
            part[key] = value
            part.move_to_end(key)
            self._data.move_to_end(ns)
            while len(part) > bound:
                part.popitem(last=False)
            while len(self._data) > MAX_NAMESPACES:
                self._data.popitem(last=False)

    def pop(self, ns: str, key: str) -> None:
        with self._lock:
            part = self._data.get(ns)
            if part is not None:
                part.pop(key, None)

    def forget(self, ns: str) -> None:
        with self._lock:
            self._data.pop(ns, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def namespaces(self) -> list[str]:
        with self._lock:
            return list(self._data)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(p) for p in self._data.values())


_REGISTRY: list[NamespacedLRU] = []


def forget_namespace(ns: str) -> None:
    """Drop every cached result computed from one database (the demo calls it when closing a workspace)."""
    for cache in _REGISTRY:
        cache.forget(ns)

"""The service-wide storage budget, enforced with reservations (thread-safe).

Admission rule, checked atomically for every piece of work that can write to disk:

    measured usage + outstanding reservations + requested bytes + head-room <= budget

- **Measured usage** is everything under the demo's data folder (workspaces, seed, UI copy, temporary files,
  and, from Stage 3, snapshot archives), read fresh on each admission. Finished writes are therefore always
  counted at their real size, not at the size a request declared. Bytes an in-progress operation has already
  written are counted twice (on disk and in its reservation), which errs on the side of refusing.
- **A reservation** is taken before work is accepted and released exactly once (`release` is idempotent), only
  when that work can no longer write: on success, failure, cancellation, and only after work that outlived the
  visitor's request (a front-door timeout) has really ended.
- **Head-room** covers what a reservation cannot bound in advance (a request writing more than its estimate,
  short-lived files of read-only requests).

Lock order: the budget lock is a leaf. It is held only for the admission arithmetic and the bookkeeping, never
while awaiting and never while taking another lock, so it cannot take part in a deadlock. Callers may hold the
workspace manager's guard or a workspace lock when they reserve or release.
"""

import itertools
import threading
from collections.abc import Callable


class Reservation:
    """Bytes held against the budget for one piece of work."""

    __slots__ = ("_budget", "id", "nbytes", "purpose", "released")

    def __init__(self, budget: "StorageBudget", rid: int, nbytes: int, purpose: str) -> None:
        self._budget = budget
        self.id = rid
        self.nbytes = nbytes
        self.purpose = purpose
        self.released = False

    def release(self) -> None:
        self._budget._release(self)


class StorageBudget:
    def __init__(self, limit_bytes: int, headroom_bytes: int, measure: Callable[[], int]) -> None:
        self.limit_bytes = limit_bytes
        self.headroom_bytes = headroom_bytes
        self._measure = measure
        self._lock = threading.Lock()
        self._held: dict[int, Reservation] = {}
        self._ids = itertools.count(1)

    def reserve(self, nbytes: int, purpose: str) -> Reservation | None:
        """A reservation of `nbytes`, or None (nothing reserved) if it does not fit the budget."""
        if nbytes < 0:
            raise ValueError("a reservation cannot be negative")
        with self._lock:
            used = self._measure()
            held = sum(r.nbytes for r in self._held.values())
            if used + held + nbytes + self.headroom_bytes > self.limit_bytes:
                return None
            res = Reservation(self, next(self._ids), nbytes, purpose)
            self._held[res.id] = res
            return res

    def _release(self, res: Reservation) -> None:
        with self._lock:
            if not res.released:
                res.released = True
                self._held.pop(res.id, None)

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(r.nbytes for r in self._held.values())

    @property
    def outstanding(self) -> int:
        with self._lock:
            return len(self._held)

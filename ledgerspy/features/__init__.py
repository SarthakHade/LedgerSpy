"""P6: the single forensic feature registry.

Every feature LedgerSpy computes is registered exactly once (`ledgerspy.features.spec.REGISTRY`) with its
version, meaning, inputs, point-in-time rule, peer group, missing-data policy and one implementation.
Detectors (P7, P8), the risk engine (P9) and models (P10) consume feature matrices built from this registry;
none may compute a feature of its own. See docs/feature-registry.md.
"""

# Importing the modules registers their features.
from ledgerspy.features import deferred, entity, transaction  # noqa: F401
from ledgerspy.features.spec import REGISTRY  # noqa: F401

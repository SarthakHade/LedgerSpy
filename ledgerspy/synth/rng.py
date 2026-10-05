"""Deterministic randomness.

Every component draws from its own stream derived from (seed, name), so adding a vendor or a scenario does not
shift the random numbers of unrelated parts. Streams use `random.Random` seeded with an integer from SHA-256,
which is independent of PYTHONHASHSEED. (Bit-identity across Python versions/platforms is not promised:
`random`'s float transforms use the platform libm. The manifest records the Python version.)
"""

import hashlib
import math
import random


def stream(seed: int, name: str) -> random.Random:
    digest = hashlib.sha256(f"ledgerspy-synth:{seed}:{name}".encode()).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def poisson(rng: random.Random, lam: float) -> int:
    """Knuth's method; adequate for the small rates used here."""
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def lognormal_minor(rng: random.Random, median_minor: int, sigma: float) -> int:
    """A log-normal amount in minor units around `median_minor`."""
    return max(100, round(median_minor * math.exp(rng.gauss(0.0, sigma))))

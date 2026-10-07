"""Leakage-safe train / validation / test splits (deterministic, split FIRST; nothing is fitted before).

- `random_stratified`: rows are shuffled (seeded) within each class and cut by the configured fractions. A
  row-level split: rows of one counterparty or one injected scenario can land on both sides — reported as a
  leakage risk.
- `chronological`: rows ordered by posting time (ties: txn_id) and cut in time order; rows sharing a timestamp
  stay on one side. No training row is later than a validation row, and none of those is later than a test
  row. Rows without a timestamp are excluded (counted).
- `group`: connected components of rows that share a counterparty stream or an injected scenario (union–find)
  are assigned whole, so no counterparty and no scenario appears in two splits. Components containing a
  positive and those without are shuffled (seeded) and cut separately, to keep positives in every split
  where possible.

Every split reports its sizes, positives and the overlap of counterparties / scenarios between train and
test (the measurable leakage risk of row-level and time-level splits).
"""

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ledgerspy.config.schema import SupervisedSplits
from ledgerspy.supervised.dataset import TrainingData

PARTS = ("train", "validation", "test")


@dataclass
class Split:
    strategy: str
    parts: dict[str, list[int]]  # part → row indices (sorted)
    excluded: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    clusters: list[str] | None = None  # per row: the resampling cluster for bootstrap intervals (None = rows)

    def describe(self, data: TrainingData) -> dict[str, Any]:
        sizes = {p: len(v) for p, v in self.parts.items()}
        pos = {p: sum(data.y[i] for i in v) for p, v in self.parts.items()}
        ents = {p: {data.entity_groups[i] for i in v} for p, v in self.parts.items()}
        scen = {p: {s for i in v for s in data.scenario_groups[i]} for p, v in self.parts.items()}
        out = {
            "strategy": self.strategy,
            "rows": sizes,
            "positives": pos,
            "excluded_rows": self.excluded,
            "counterparties_in_train_and_test": len(ents["train"] & ents["test"]),
            "scenarios_in_train_and_test": sorted(scen["train"] & scen["test"]),
            **self.details,
        }
        if data.timestamps and self.strategy == "chronological":
            out["time_ranges"] = {
                p: [
                    min(data.timestamps[i] for i in v).isoformat(),
                    max(data.timestamps[i] for i in v).isoformat(),
                ]
                for p, v in self.parts.items()
                if v
            }
        return out


def _cut(n: int, cfg: SupervisedSplits) -> tuple[int, int]:
    a = round(n * cfg.train)
    b = round(n * (cfg.train + cfg.validation))
    return a, b


def random_stratified(data: TrainingData, cfg: SupervisedSplits, seed: int) -> Split:
    parts: dict[str, list[int]] = {p: [] for p in PARTS}
    for cls in (0, 1):
        idx = sorted((i for i in range(data.n) if data.y[i] == cls), key=lambda i: data.txn_ids[i])
        random.Random(f"{seed}:{cls}").shuffle(idx)  # noqa: S311 — reproducible split, not security
        a, b = _cut(len(idx), cfg)
        parts["train"] += idx[:a]
        parts["validation"] += idx[a:b]
        parts["test"] += idx[b:]
    return Split("random_stratified", {p: sorted(v) for p, v in parts.items()})


def chronological(data: TrainingData, cfg: SupervisedSplits, seed: int) -> Split:
    dated = sorted(
        (i for i in range(data.n) if data.timestamps[i] is not None),
        key=lambda i: (data.timestamps[i], data.txn_ids[i]),
    )
    excluded = data.n - len(dated)
    a, b = _cut(len(dated), cfg)

    def tie_safe(k: int) -> int:  # move the cut forward to the end of a block of equal timestamps
        while 0 < k < len(dated) and data.timestamps[dated[k]] == data.timestamps[dated[k - 1]]:
            k += 1
        return k

    a = tie_safe(a)
    b = max(tie_safe(b), a)
    return Split(
        "chronological",
        {"train": sorted(dated[:a]), "validation": sorted(dated[a:b]), "test": sorted(dated[b:])},
        excluded=excluded,
        clusters=[
            data.timestamps[i].strftime("%Y-%m-%d") if data.timestamps[i] else "none" for i in range(data.n)
        ],
    )


def group(data: TrainingData, cfg: SupervisedSplits, seed: int) -> Split:
    parent = list(range(data.n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first: dict[str, int] = {}
    for i in range(data.n):
        for key in [data.entity_groups[i], *("scenario:" + s for s in data.scenario_groups[i])]:
            if key in first:
                union(i, first[key])
            else:
                first[key] = i
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(data.n):
        comps[find(i)].append(i)
    parts: dict[str, list[int]] = {p: [] for p in PARTS}
    for has_pos in (False, True):
        bucket = sorted(
            (c for c in comps.values() if any(data.y[i] for i in c) == has_pos),
            key=lambda c: min(data.txn_ids[i] for i in c),
        )
        random.Random(f"{seed}:group:{int(has_pos)}").shuffle(bucket)  # noqa: S311 — reproducible split
        total = sum(len(c) for c in bucket)
        seen = 0
        for c in bucket:
            mid = (seen + len(c) / 2) / total if total else 0
            part = (
                "train" if mid < cfg.train else "validation" if mid < cfg.train + cfg.validation else "test"
            )
            parts[part] += c
            seen += len(c)
    sizes = sorted((len(c) for c in comps.values()), reverse=True)
    return Split(
        "group",
        {p: sorted(v) for p, v in parts.items()},
        details={
            "components": len(comps),
            "largest_components": sizes[:5],
            "grouping": "counterparty stream ∪ injected scenario (connected components)",
        },
        clusters=[f"c{find(i)}" for i in range(data.n)],
    )


STRATEGIES = {"random_stratified": random_stratified, "chronological": chronological, "group": group}


def make(strategy: str, data: TrainingData, cfg: SupervisedSplits, seed: int) -> Split:
    return STRATEGIES[strategy](data, cfg, seed)

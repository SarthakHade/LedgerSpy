"""Feature-quality report: computed facts about a feature run, nothing else.

For every registered feature of a level: status (computed / not_available / not_available_yet) and why,
version, point-in-time semantics, row count, nulls and missing share, distinct values, variance and min/max
(numeric and boolean as 0/1), and for peer-fallback features the count of rows per basis (incl. `unavailable`
= insufficient history). Flags (constant, excessive_missing, insufficient_history) are reported; nothing is
dropped.
"""

from collections import Counter
from typing import Any

from ledgerspy.config.schema import FeaturesConfig
from ledgerspy.features.matrix import FeatureMatrix, FeatureRun
from ledgerspy.features.spec import REGISTRY, Kind


def _numeric(values: list[Any]) -> list[float]:
    return [float(v) for v in values if v is not None and not isinstance(v, str)]


def column_stats(spec_kind: Kind, values: list[Any]) -> dict[str, Any]:
    n = len(values)
    present = [v for v in values if v is not None]
    out: dict[str, Any] = {
        "rows": n,
        "nulls": n - len(present),
        "missing_share": round((n - len(present)) / n, 6) if n else None,
        "distinct": len(set(present)),
    }
    nums = _numeric(present) if spec_kind in (Kind.FLOAT, Kind.INT, Kind.BOOL) else []
    if nums:
        mean = sum(nums) / len(nums)
        out.update(
            min=min(nums),
            max=max(nums),
            mean=round(mean, 6),
            variance=round(sum((x - mean) ** 2 for x in nums) / len(nums), 6),
        )
    elif present and spec_kind in (Kind.CATEGORY, Kind.DATE):
        out.update(min=min(present), max=max(present), top=Counter(present).most_common(3))
    return out


def level_report(matrix: FeatureMatrix, cfg: FeaturesConfig) -> list[dict[str, Any]]:
    rows = []
    for name, st in sorted(matrix.status.items()):
        spec = REGISTRY[name]
        entry: dict[str, Any] = {
            "feature": name,
            "version": spec.version if spec.status == "implemented" else None,
            "family": spec.family,
            "semantics": spec.semantics.value,
            "status": st["status"],
            "reason": st["reason"],
        }
        if st["status"] == "computed":
            stats = column_stats(spec.kind, matrix.columns[name])
            entry.update(stats)
            flags = []
            if stats["distinct"] < cfg.report.min_distinct_values:
                flags.append("constant")
            if stats["missing_share"] is not None and stats["missing_share"] > cfg.report.max_missing_share:
                flags.append("excessive_missing")
            if name in matrix.basis:
                basis = Counter(b for b in matrix.basis[name] if b is not None)
                entry["basis"] = dict(sorted(basis.items()))
                if basis.get("unavailable"):
                    flags.append("insufficient_history")
            entry["flags"] = flags
        rows.append(entry)
    return rows


def quality_report(run: FeatureRun, cfg: FeaturesConfig) -> dict[str, Any]:
    tx = level_report(run.transactions, cfg)
    ent = level_report(run.entities, cfg)
    every = tx + ent

    def count(status: str) -> int:
        return sum(1 for e in every if e["status"] == status)

    return {
        "dataset": run.manifest["dataset"],
        "registry_sha256": run.manifest["registry_sha256"],
        "summary": {
            "registered": len(every),
            "computed": count("computed"),
            "not_available": count("not_available"),
            "not_available_yet": count("not_available_yet"),
            "transaction_rows": len(run.transactions.subjects),
            "entity_subjects": len(run.entities.subjects),
            "constant": sorted(e["feature"] for e in every if "constant" in e.get("flags", [])),
            "excessive_missing": sorted(
                e["feature"] for e in every if "excessive_missing" in e.get("flags", [])
            ),
            "insufficient_history": sorted(
                e["feature"] for e in every if "insufficient_history" in e.get("flags", [])
            ),
        },
        "transaction_features": tx,
        "entity_features": ent,
    }

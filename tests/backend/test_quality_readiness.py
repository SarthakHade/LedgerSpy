"""P4: severity assignment and the readiness formula as pure, deterministic functions."""

import pytest

from ledgerspy.config import load_config
from ledgerspy.quality import readiness, severity
from ledgerspy.quality.readiness import ScoredIssue

CFG = load_config().settings.quality
R = CFG.readiness
ALL = {c: True for comp in R.components.values() for c in comp.checks}


# ---------- severity ----------


@pytest.mark.parametrize(
    ("check", "affected", "total", "synthetic", "expected"),
    [
        ("missing_required", 1, 100, False, "error"),  # base
        ("missing_required", 4, 100, False, "error"),  # 4% < 5%
        ("missing_required", 5, 100, False, "critical"),  # 5% ≥ 5% → critical
        ("missing_optional", 19, 100, False, "info"),
        ("missing_optional", 20, 100, False, "warning"),
        ("exact_duplicate", 0, 100, False, "warning"),
        ("exact_duplicate", 1, 100, False, "error"),  # 1% ≥ 1%
        ("negative_amount", 90, 100, False, "info"),  # no escalation rule: legitimate reversals
        ("balance_continuity", 1, 100, False, "warning"),
        ("balance_continuity", 50, 100, False, "error"),
        ("balance_continuity", 50, 100, True, "info"),  # synthetic override is final
        ("unmapped_columns", 0, 100, False, "info"),
    ],
)
def test_severity_rules(check, affected, total, synthetic, expected):
    assert severity.assign(check, affected, total, synthetic, CFG.severity)["severity"] == expected


def test_severity_explains_itself():
    s = severity.assign("zero_amount", 10, 100, False, CFG.severity)
    assert s == {
        "severity": "error",
        "base": "warning",
        "share": 0.1,
        "rule": "affected share 0.1000 >= 0.05 → error",
    }


def test_every_check_has_a_base_severity_in_config():
    from ledgerspy.config.schema import QUALITY_CHECKS

    assert set(CFG.severity.base) == set(QUALITY_CHECKS)


# ---------- readiness ----------


def test_no_issues_is_exactly_100_and_ready():
    out = readiness.compute(10, ALL, [], R)
    assert out["score"] == 100.0 and out["status"] == "ready" and not out["capped_by_blocking_issues"]
    assert all(c["clean_share"] == 1.0 for c in out["components"])


def test_hand_computed_example():
    # 10 rows. completeness: rows 1, 2 (error); validity: row 3 (warning); consistency: row 4 (info → ignored)
    issues = [
        ScoredIssue("missing_required", "error", 1),
        ScoredIssue("missing_required", "error", 1),  # same row counts once
        ScoredIssue("missing_required", "error", 2),
        ScoredIssue("malformed_identifier", "warning", 3),
        ScoredIssue("near_duplicate", "info", 4),
    ]
    out = readiness.compute(10, ALL, issues, R)
    # 0.35*0.8 + 0.30*0.9 + 0.20*1 + 0.15*1 = 0.28 + 0.27 + 0.20 + 0.15 = 0.90
    assert out["raw"] == pytest.approx(0.90)
    assert out["score"] == 90.0 and out["status"] == "not_ready"
    comp = {c["component"]: c for c in out["components"]}
    assert comp["completeness"]["affected_rows"] == 2 and comp["completeness"][
        "points_lost"
    ] == pytest.approx(7.0)
    assert comp["validity"]["points_lost"] == pytest.approx(3.0)
    assert comp["consistency"]["affected_rows"] == 0  # the INFO issue does not count
    assert sum(c["points"] for c in out["components"]) == pytest.approx(90.0)


def test_never_100_while_blocking_issues_remain():
    # one error row in 1,000,000 rows: the raw score rounds to 100.0, but it is capped and not ready
    out = readiness.compute(1_000_000, ALL, [ScoredIssue("missing_required", "error", 7)], R)
    assert out["raw"] > 0.9999 and out["score"] == 99.9  # floor rounding already keeps it below 100
    assert out["status"] == "not_ready" and not out["capped_by_blocking_issues"]


def test_dataset_level_blocking_issue_is_capped():
    # a blocking issue without a row leaves every component clean (raw = 1.0): only the cap prevents 100
    out = readiness.compute(10, ALL, [ScoredIssue("missing_required", "critical", None)], R)
    assert out["raw"] == 1.0
    assert out["score"] == 99.9 and out["capped_by_blocking_issues"] and out["status"] == "not_ready"


def test_floor_rounding_never_rounds_up():
    # one warning row in 2000 rows of the consistency component: raw = 1 - 0.15/2000 = 0.999925
    out = readiness.compute(2000, ALL, [ScoredIssue("zero_amount", "warning", 1)], R)
    assert out["score"] == 99.9 and out["status"] == "ready_with_warnings"


def test_unassessed_components_are_excluded_and_reported():
    available = {
        **ALL,
        "balance_continuity": False,
        "out_of_period": False,
        "zero_amount": False,
        "negative_amount": False,
        "near_duplicate": False,
    }
    out = readiness.compute(10, available, [ScoredIssue("missing_required", "error", 1)], R)
    comp = {c["component"]: c for c in out["components"]}
    assert comp["consistency"]["assessed"] is False and comp["consistency"]["points"] is None
    assert out["assessed_weight"] == pytest.approx(0.85)
    # 0.35*0.9 + 0.30 + 0.20 = 0.815 → / 0.85
    assert out["raw"] == pytest.approx(0.815 / 0.85)


def test_readiness_is_deterministic():
    issues = [ScoredIssue("exact_duplicate", "error", r) for r in (3, 1, 2)]
    first = readiness.compute(50, ALL, issues, R)
    assert all(readiness.compute(50, ALL, list(reversed(issues)), R) == first for _ in range(5))


def test_readiness_needs_rows():
    with pytest.raises(ValueError):
        readiness.compute(0, ALL, [], R)

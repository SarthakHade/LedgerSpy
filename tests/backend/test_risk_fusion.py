"""P9 fusion, caps, tiers, review-volume guard and monotonicity (mandatory sanity rules)."""

import itertools
import math
import random

import pytest

from ledgerspy.config import ConfigError, load_config
from ledgerspy.risk import calibration
from ledgerspy.risk.fusion import apportion
from tests.backend.risk_support import fused, settings

DUP = ("S-dup", "duplicate_exact", 0.6)
VEL = ("S-vel", "velocity", 0.4)
IF = ("S-if", "isolation_forest", 0.3)
PEER = ("S-peer", "robust_peer", 0.4)
TAIL = ("S-tail", "tail_percentile", 0.2)


def total(f) -> float:
    return sum(s.contribution for fam in f.families for s in fam.signals)


# --------------------------------------------------------------------------------------------- formula


def test_single_signal_scores_its_evidence():
    f = fused({"duplicate": [DUP]})
    assert f.score == pytest.approx(60.0) and f.score_display == 60.0 and f.sources == 1


def test_independent_sources_combine_by_noisy_or():
    f = fused({"duplicate": [DUP], "transaction_pattern": [VEL]})
    assert f.score == pytest.approx(100 * (1 - 0.4 * 0.6))
    assert f.sources == 2


def test_contributions_reconcile_exactly_to_the_score():
    f = fused(
        {
            "duplicate": [DUP],
            "transaction_pattern": [VEL, ("S-v2", "dormancy", 0.4)],
            "statistical_anomaly": [IF],
            "peer_anomaly": [PEER],
            "tail_anomaly": [TAIL],
        }
    )
    assert total(f) == pytest.approx(f.score, abs=1e-9)
    for g in f.groups:
        fams = [x for x in f.families if x.family in g.families]
        assert sum(x.contribution for x in fams) == pytest.approx(g.contribution, abs=1e-9)
    # displayed values (hundredths) add up exactly to the displayed score
    shown = [s.contribution_display for fam in f.families for s in fam.signals]
    assert round(sum(round(x * 100) for x in shown)) == round(f.score_display * 100)
    assert sum(round(fam.contribution_display * 100) for fam in f.families) == round(f.score_display * 100)


def test_score_is_bounded_whatever_the_evidence():
    many = {
        name: [(f"S-{name}-{i}", d, 1.0) for i in range(30)]
        for name, d in [
            ("duplicate", "duplicate_exact"),
            ("transaction_pattern", "velocity"),
            ("temporal", "temporal"),
            ("entity_evidence", "round_amount"),
            ("benford", "benford_subgroup"),
            ("statistical_anomaly", "isolation_forest"),
            ("peer_anomaly", "robust_peer"),
            ("tail_anomaly", "tail_percentile"),
        ]
    }
    f = fused(many)
    assert 0 < f.score < 100 and total(f) == pytest.approx(f.score)


def test_largest_remainder_rounding_is_exact_and_deterministic():
    parts = [100 / 3, 100 / 3, 100 / 3]
    assert apportion(parts, 100.0) == [33.34, 33.33, 33.33]
    assert apportion([0.001, 0.002], 0.003) == [0.0, 0.0]


# --------------------------------------------------------------------------------------------- caps


def test_family_cap_holds_however_many_correlated_signals():
    one = fused({"tail_anomaly": [TAIL]})
    many = fused({"tail_anomaly": [(f"S-t{i}", "tail_percentile", 0.2) for i in range(40)]})
    fam = many.families[0]
    assert fam.cap_applied and fam.capped == 0.2 and many.score == pytest.approx(20.0)
    assert one.score == pytest.approx(20.0)  # already at the cap


def test_within_family_diminishing_returns():
    f = fused({"transaction_pattern": [VEL, ("S-vz", "dormancy", 0.4)]})
    assert f.score == pytest.approx(100 * (1 - 0.6 * (1 - 0.4 * 0.5)))
    s = {x.signal_id: x for x in f.families[0].signals}
    assert s["S-vz"].rank == 1 and s["S-vz"].effective == pytest.approx(0.2)


def test_correlated_anomaly_families_cannot_create_an_extreme_score_alone():
    strong = fused(
        {
            "statistical_anomaly": [("S-a", "isolation_forest", 1.0)] * 1,
            "peer_anomaly": [(f"S-p{i}", "robust_peer", 1.0) for i in range(20)],
            "tail_anomaly": [(f"S-t{i}", "tail_percentile", 1.0) for i in range(20)],
        }
    )
    assert strong.score == pytest.approx(45.0)  # group cap 0.45
    assert strong.sources == 1 and strong.groups[0].cap_applied
    tier, _ = calibration.tier(strong.score_display, strong.sources, settings().risk.tiers)
    assert tier == "medium"


def test_one_family_cannot_overwhelm_other_evidence():
    f = fused(
        {
            "duplicate": [(f"S-d{i}", "duplicate_exact", 1.0) for i in range(10)],
            "temporal": [("S-t", "temporal", 0.4)],
        }
    )
    shares = {x.family: x.contribution / f.score for x in f.families}
    assert shares["duplicate"] < 0.7 and shares["temporal"] > 0.3


def test_data_quality_is_reported_but_never_scored():
    f = fused({"data_quality": [("S-bal", "balance_continuity", 0.4), ("S-bal2", "balance_continuity", 0.4)]})
    assert f.score == 0.0 and f.sources == 0
    fam = f.families[0]
    assert not fam.scored and fam.raw > 0 and fam.contribution == 0
    assert calibration.tier(f.score_display, f.sources, settings().risk.tiers)[0] == "low"
    both = fused({"data_quality": [("S-bal", "balance_continuity", 0.4)], "duplicate": [DUP]})
    assert both.score == pytest.approx(60.0)  # data quality adds nothing


def test_unknown_family_or_overlapping_detectors_are_configuration_errors():
    with pytest.raises(ConfigError, match="two risk families"):
        load_config(
            overrides={"risk": {"families": {"benford": {"detectors": ["benford_subgroup", "velocity"]}}}}
        )
    with pytest.raises(ConfigError, match="unscored family"):
        load_config(overrides={"risk": {"families": {"data_quality": {"cap": 0.2}}}})


# --------------------------------------------------------------------------------------------- monotonicity

FAMILIES = {
    "duplicate": "duplicate_exact",
    "transaction_pattern": "velocity",
    "temporal": "temporal",
    "entity_evidence": "round_amount",
    "benford": "benford_subgroup",
    "statistical_anomaly": "isolation_forest",
    "peer_anomaly": "robust_peer",
    "tail_anomaly": "tail_percentile",
    "data_quality": "balance_continuity",
}


def random_case(rng: random.Random, n: int) -> dict:
    out: dict = {}
    for i in range(n):
        fam = rng.choice(sorted(FAMILIES))
        out.setdefault(fam, []).append((f"S-{i:03d}", FAMILIES[fam], round(rng.random(), 3)))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_adding_a_signal_never_decreases_and_removing_never_increases(seed):
    rng = random.Random(seed)  # noqa: S311 — test data
    base = random_case(rng, rng.randint(1, 8))
    s0 = fused(base).score
    fam = rng.choice(sorted(FAMILIES))
    added = {k: list(v) for k, v in base.items()}
    added.setdefault(fam, []).append(("S-new", FAMILIES[fam], rng.random()))
    assert fused(added).score >= s0 - 1e-12
    for k in base:
        removed = {kk: [x for x in v if not (kk == k and x == base[k][0])] for kk, v in base.items()}
        assert fused({kk: v for kk, v in removed.items() if v}).score <= s0 + 1e-12


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_strengthening_a_signal_never_decreases_the_score(family):
    for e in (0.05, 0.2, 0.5):
        weak = fused({family: [("S-a", FAMILIES[family], e)], "temporal": [("S-t", "temporal", 0.3)]}).score
        strong = fused(
            {family: [("S-a", FAMILIES[family], min(2 * e, 1.0))], "temporal": [("S-t", "temporal", 0.3)]}
        ).score
        assert strong >= weak - 1e-12


def test_capped_family_stays_capped_when_strengthened():
    caps = settings().risk.families
    for fam in ("duplicate", "temporal", "benford"):
        f = fused({fam: [(f"S-{i}", FAMILIES[fam], 1.0) for i in range(5)]})
        assert f.families[0].capped == caps[fam].cap and f.score == pytest.approx(100 * caps[fam].cap)


def test_no_evidence_means_no_risk():
    f = fused({})
    assert f.score == 0.0 and f.families == [] and f.sources == 0
    assert fused({"duplicate": [("S-z", "duplicate_exact", 0.0)]}).score == 0.0


def test_order_of_signals_does_not_matter():
    items = [DUP, ("S-d2", "duplicate_near", 0.5)]
    a = fused({"duplicate": items})
    b = fused({"duplicate": list(reversed(items))})
    assert a.to_dict() == b.to_dict()


# --------------------------------------------------------------------------------------------- tiers + guard


def test_tiers_need_score_and_independent_corroboration():
    t = settings().risk.tiers
    assert calibration.tier(60.0, 1, t)[0] == "medium"  # one strong source is not high
    assert calibration.tier(60.0, 2, t)[0] == "high"
    assert calibration.tier(80.0, 2, t)[0] == "high"  # critical needs 3 sources
    assert calibration.tier(80.0, 3, t)[0] == "critical"
    assert calibration.tier(24.99, 3, t)[0] == "low"
    assert "corroboration" in calibration.tier(60.0, 1, t)[1]


def test_a_single_benign_large_payment_is_not_critical():
    f = fused(
        {"tail_anomaly": [("S-t", "tail_percentile", 0.2)], "peer_anomaly": [("S-p", "robust_peer", 0.4)]}
    )
    assert calibration.tier(f.score_display, f.sources, settings().risk.tiers)[0] in ("low", "medium")


def rows(n_high: int, n_low: int) -> list[dict]:
    out = []
    for i in range(n_high):
        out.append(
            {
                "txn_id": f"T{i:04d}",
                "status": "scored",
                "score": 70.0 - i * 0.01,
                "tier": "high",
                "independent_sources": 2,
                "amount_minor": 100,
            }
        )
    for i in range(n_low):
        out.append(
            {
                "txn_id": f"L{i:04d}",
                "status": "scored",
                "score": 0.0,
                "tier": "low",
                "independent_sources": 0,
                "amount_minor": 100,
            }
        )
    return out


def test_volume_guard_warns_on_a_mis_weighted_configuration_without_altering_scores():
    r = rows(300, 700)  # 30% high: far beyond a 2% budget
    before = [(x["score"], x["tier"]) for x in r]
    guard = calibration.review(r, settings().risk.review)
    assert guard["calibration_warning"] and guard["exceeded_high_budget"] and guard["high_budget"] == 20
    assert [(x["score"], x["tier"]) for x in r] == before  # raw scores and tiers preserved
    assert sum(1 for x in r if x["review_priority"] == "review_queue") == 250
    assert sum(1 for x in r if x["review_priority"] == "over_capacity") == 50
    assert [x["review_rank"] for x in r[:3]] == [1, 2, 3]  # ranked by score within the tier
    assert r[-1]["review_rank"] is None and r[-1]["review_priority"] == "none"


def test_volume_guard_quiet_within_budget_and_deterministic():
    r1, r2 = rows(5, 995), list(reversed(rows(5, 995)))
    g1 = calibration.review(r1, settings().risk.review)
    g2 = calibration.review(r2, settings().risk.review)
    assert not g1["calibration_warning"] and g1 == g2
    assert {x["txn_id"]: x["review_rank"] for x in r1} == {x["txn_id"]: x["review_rank"] for x in r2}


def test_review_ranking_breaks_ties_by_sources_amount_then_id():
    r = [
        {
            "txn_id": "B",
            "status": "scored",
            "score": 50.0,
            "tier": "high",
            "independent_sources": 2,
            "amount_minor": 10,
        },
        {
            "txn_id": "A",
            "status": "scored",
            "score": 50.0,
            "tier": "high",
            "independent_sources": 2,
            "amount_minor": 10,
        },
        {
            "txn_id": "C",
            "status": "scored",
            "score": 50.0,
            "tier": "high",
            "independent_sources": 2,
            "amount_minor": 99,
        },
        {
            "txn_id": "D",
            "status": "scored",
            "score": 50.0,
            "tier": "high",
            "independent_sources": 3,
            "amount_minor": 1,
        },
    ]
    calibration.review(r, settings().risk.review)
    assert sorted(r, key=lambda x: x["review_rank"])[0]["txn_id"] == "D"
    assert [x["txn_id"] for x in sorted(r, key=lambda x: x["review_rank"])] == ["D", "C", "A", "B"]


def test_exhaustive_small_subsets_reconcile():
    pool = [DUP, VEL, IF, PEER, TAIL, ("S-tmp", "temporal", 0.4), ("S-b", "benford_subgroup", 0.2)]
    fam_of = {
        "duplicate_exact": "duplicate",
        "velocity": "transaction_pattern",
        "isolation_forest": "statistical_anomaly",
        "robust_peer": "peer_anomaly",
        "tail_percentile": "tail_anomaly",
        "temporal": "temporal",
        "benford_subgroup": "benford",
    }
    for k in range(1, len(pool) + 1):
        for combo in itertools.combinations(pool, k):
            case: dict = {}
            for x in combo:
                case.setdefault(fam_of[x[1]], []).append(x)
            f = fused(case)
            assert math.isclose(total(f), f.score, abs_tol=1e-9)
            assert sum(round(fam.contribution_display * 100) for fam in f.families) == round(
                f.score_display * 100
            )

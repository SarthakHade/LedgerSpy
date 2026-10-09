"""P9 guard: the frontend renders API results and contains no risk scoring, labels or fabricated values.

A static check over frontend/: if scoring formulas, thresholds, random numbers, label columns or hard-coded
results come back, this fails. (The Node harness and browser check exercise the same page dynamically.)
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend"
FILES = sorted(p for p in FE.rglob("*") if p.is_file() and p.suffix in (".js", ".html", ".css"))
JS = [p for p in FILES if p.suffix == ".js"]


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def code(p: Path) -> str:
    """JS without comments (comments may describe what is NOT done)."""
    s = re.sub(r"/\*[\s\S]*?\*/", "", text(p))
    return re.sub(r"(^|[^:'\"\\])//.*$", r"\1", s, flags=re.M)


def test_the_legacy_scoring_engine_is_gone():
    names = {p.name for p in JS}
    assert "ml-engine.js" not in names and "data-pipeline.js" not in names
    assert "ml-engine.js" not in text(FE / "index.html")


@pytest.mark.parametrize(
    "pattern",
    [
        r"Math\.random",
        r"sigmoid",
        r"HEURISTIC_",
        r"riskScore",
        r"predictTransaction|engineerFeatures|tierForScore|heuristicRules|runMLOnData",
        r"isFraud|isFlaggedFraud|labelColumns",
        r"\bthresholds\s*:",
        r"reviewBudgetPct|benfordExpectedPct|benfordOverrepPP",
        r"Math\.exp\(",  # no logistic / fusion arithmetic in the browser
    ],
)
def test_no_scoring_code_or_labels_in_frontend_js(pattern):
    hits = [p.name for p in JS if re.search(pattern, code(p), re.I if "Fraud" in pattern else 0)]
    assert not hits, (pattern, hits)


def test_no_score_or_tier_decided_by_comparing_numbers():
    """A tier or flag must come from the API, never from comparing a score with a constant in the browser."""
    for p in JS:
        src = code(p)
        assert not re.search(r"score\w*\s*(>=|<=|>|<)\s*\d", src, re.I), p.name
        assert not re.search(r"\d\s*(>=|<=|>|<)\s*\w*score", src, re.I), p.name
        assert not re.search(r"tier\s*=\s*['\"](critical|high|medium|low)['\"]", src), p.name


def test_no_hard_coded_results_in_the_page():
    html = text(FE / "index.html")
    visible = re.sub(r"<(script|style)[\s\S]*?</\1>", "", html)
    visible = re.sub(r'\sstyle="[^"]*"', "", visible)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert not re.search(r"\d+(\.\d+)?\s*%", visible), re.findall(r".{20}\d+(?:\.\d+)?\s*%.{10}", visible)[:3]
    assert not re.search(r"₹\s*\d", visible)
    for literal in ("93.7%", "₹2.45 Cr", "87%", "Risk = ", "court-ready", "SHAP-style", "calibrated v2"):
        assert literal not in html


def test_every_api_value_is_rendered_as_text():
    """innerHTML is only used by the chatbot for its own markup with escaped values (as in P1)."""
    for p in JS:
        for i, line in enumerate(text(p).splitlines(), start=1):
            if re.search(r"\.innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=|document\.write", line):
                allowed = p.name == "chatbot-ui.js" and (
                    "msg-label\">Assistant</div>' + html" in line
                    or "<span></span><span></span><span></span>" in line
                )
                assert allowed, f"{p.name}:{i}"


def test_chatbot_escapes_every_api_value():
    src = code(FE / "js" / "chatbot-ui.js")
    body = src[src.index("async function processQuery") :]
    # every string concatenation that inserts a variable goes through E(...) (escapeHtml)
    risky = re.findall(r"\+\s*(?!E\(|fams\b|lines\b|\(lines|rows\.map|u\.length|'|\")[a-zA-Z_][\w.]*", body)
    assert not risky, risky[:5]


def test_no_external_network_dependencies():
    for p in FILES:
        for url in re.findall(r"https?://[^\s\"'`)<>]+", text(p)):
            assert url.startswith("http://www.w3.org/2000/svg"), (p.name, url)


def test_unavailable_modules_are_labelled_not_simulated():
    html = text(FE / "index.html")
    for module in (
        "Bank statement reconciliation",
        "Monte Carlo",
        "Industry Benchmark",
        "Entity graph not available",
    ):
        assert module.lower() in html.lower(), module
    # P14b: bank reconciliation (incl. PDF / OCR) is built; its card now states the local OCR engine's status
    # from the API instead of "not available". Monte Carlo, benchmarks and the pre-analysis graph remain.
    assert html.count('class="na-card"') >= 3
    assert 'id="bsr-pdf-status"' in html and "/api/pdf/ocr-status" in text(FE / "js" / "recon-ui.js")


def test_the_frontend_reads_risk_only_from_the_api():
    ui = code(FE / "js" / "risk-ui.js")
    for endpoint in ("/risk'", "/detectors", "/scatter", "/entities", "/transactions"):
        assert endpoint in ui or endpoint.strip("'") in ui, endpoint
    assert "LS_API.available" in ui  # without the local service it shows nothing computed

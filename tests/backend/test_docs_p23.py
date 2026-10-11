"""P23: the documentation describes the actual implementation.

Checks over README.md and docs/ (see docs/p23-acceptance.md):
- every relative link resolves (and same-file anchors exist);
- every `ledgerspy …` command and option, launcher option and `tools/verify.py` option in the user-facing docs
  exists in the real parsers;
- every `/api/…` endpoint they name is a registered route;
- every repository path they name exists;
- the UI labels the user guide tells the reader to click exist in the frontend;
- report types and formats match the report model, and missing formats are stated as missing;
- no machine-specific path or session token in any shipped document.
"""

import re
import sys
import unicodedata
from pathlib import Path

import pytest

from ledgerspy.api.app import create_app
from ledgerspy.cli import build_parser
from ledgerspy.config import load_config
from ledgerspy.reports import model as report_model

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packaging"))
from build_bundle import FORBIDDEN_TEXT  # noqa: E402 — the bundle builder's own scan (P21)

DOCS = REPO / "docs"
ALL_DOCS = [REPO / "README.md", *sorted(DOCS.glob("*.md"))]  # docs/audits/ is historical and not shipped
USER_DOCS = [
    REPO / "README.md",
    *(
        DOCS / n
        for n in (
            "installation.md",
            "user-guide.md",
            "operations.md",
            "security.md",
            "configuration.md",
            "developer-guide.md",
            "verification.md",
        )
    ),
]


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def code_segments(t: str) -> list[str]:
    """Fenced blocks (line by line) and inline code spans: where commands, paths and endpoints are written."""
    out: list[str] = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", t, flags=re.S):
        lines = block.replace("^\n", " ").splitlines()
        out += [ln.split(" :: ")[0] for ln in lines if ln.strip()]  # " :: " starts a bat comment
    prose = re.sub(r"```[^\n]*\n.*?```", "", t, flags=re.S)
    out += re.findall(r"`([^`\n]+)`", prose)
    return out


def slug(heading: str) -> str:
    h = unicodedata.normalize("NFKC", heading).strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


# ------------------------------------------------------------------------------------------- links


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    t = text(doc)
    anchors = {slug(h) for h in re.findall(r"^#{1,6}\s+(.+)$", t, flags=re.M)}
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", t):
        if re.match(r"[a-z]+:", target):  # http:, mailto: …
            continue
        path, _, anchor = target.partition("#")
        if not path:
            if anchor not in anchors:
                bad.append(target)
            continue
        if not (doc.parent / path).resolve().exists():
            bad.append(target)
    assert bad == []


# ------------------------------------------------------------------------------------------- commands


def _cli_tree():
    root = build_parser()

    def subs(p):
        for a in p._actions:
            if a.__class__.__name__ == "_SubParsersAction":
                return a.choices
        return {}

    return root, subs


def _check_cli(rest: str, root, subs) -> list[str]:
    words = rest.split()
    parser, path = root, []
    while words and subs(parser) and words[0] in subs(parser):
        path.append(words[0])
        parser = subs(parser)[words.pop(0)]
    problems = []
    if subs(parser) and not path:
        problems.append(f"no subcommand in: ledgerspy {rest}")
    for w in words:
        opt = w.split("=", 1)[0]
        if opt.startswith("--") and opt not in parser._option_string_actions:
            problems.append(f"ledgerspy {' '.join(path)}: unknown option {opt}")
    return problems


CLI_CALL = re.compile(r"(?<![\w.\-/\\])ledgerspy(?![\w.\-/\\])\s+([a-z][\w\-]*(?:\s+[^|&;`]*)?)")


def test_documented_cli_commands_and_options_exist():
    root, subs = _cli_tree()
    serve, doctor = subs(root)["serve"], subs(root)["doctor"]
    problems, seen = [], 0
    for doc in USER_DOCS:
        for seg in code_segments(text(doc)):
            seg = seg.split("#")[0] if seg.lstrip().startswith(("ledgerspy", ".venv")) else seg
            for m in CLI_CALL.finditer(seg):
                seen += 1
                problems += [f"{doc.name}: {p}" for p in _check_cli(m.group(1), root, subs)]
            for launcher, parser in (("ledgerspy.cmd", serve), ("doctor.cmd", doctor)):
                for m in re.finditer(re.escape(launcher) + r"((?:\s+--[\w\-]+(?:\s+(?!--)\S+)?)+)", seg):
                    for opt in re.findall(r"--[\w\-]+", m.group(1)):
                        if opt not in parser._option_string_actions:
                            problems.append(f"{doc.name}: {launcher} has no option {opt}")
    assert seen > 10  # the check found the documented commands
    assert problems == []


def test_documented_verify_commands_exist():
    src = text(REPO / "tools" / "verify.py")
    options = set(re.findall(r'add_argument\("(--[\w\-]+)"', src))
    commands = set(re.findall(r'add_parser\("([\w\-]+)"', src))
    problems, seen = [], 0
    for doc in USER_DOCS:
        for seg in code_segments(text(doc)):
            for m in re.finditer(r"tools[\\/]verify\.py((?:\s+[^\s|&;`#]+)*)", seg):
                seen += 1
                args = m.group(1).split()
                if args and not args[0].startswith("-") and args[0] not in commands:
                    problems.append(f"{doc.name}: verify.py has no command {args[0]}")
                problems += [f"{doc.name}: {a}" for a in args if a.startswith("--") and a not in options]
    assert seen >= 5
    assert problems == []


def test_documented_packaging_script_options_exist():
    problems = []
    for script in ("build_bundle.py", "make_lock.py", "offline_check.py"):
        src = text(REPO / "packaging" / script)
        options = set(re.findall(r'add_argument\(\s*"(--[\w\-]+)"', src))
        for doc in USER_DOCS:
            for seg in code_segments(text(doc)):
                for m in re.finditer(re.escape(script) + r"((?:\s+[^\s|&;`#]+)*)", seg):
                    problems += [
                        f"{doc.name}: {script} {a}"
                        for a in m.group(1).split()
                        if a.startswith("--") and a not in options
                    ]
    assert problems == []


# ------------------------------------------------------------------------------------------- endpoints


def test_documented_api_endpoints_are_registered(tmp_path):
    cfg = load_config(overrides={"workspace": {"path": str(tmp_path / "ws")}})
    app = create_app(cfg, tmp_path / "ws" / "ledgerspy.db", "t" * 40)
    routes = [r for r in app.openapi()["paths"] if r.startswith("/api")]  # includes the included routers
    assert "/api/health" in routes and len(routes) > 50
    patterns = [re.compile("^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(p)) + "$") for p in routes]
    problems, seen = [], 0
    for doc in USER_DOCS:
        for seg in code_segments(text(doc)):
            for m in re.finditer(r"(/api/[\w\-/{}.…<>]*)", seg):
                path = m.group(1).split("?")[0].rstrip("/.")
                if "…" in path:  # "/api/datasets/{id}/risk…" names a family: its stem must exist
                    stem = path.split("…")[0]
                    ok = any(r.startswith(re.sub(r"\{[^}]+\}", "", stem).rstrip("/")) for r in routes) or any(
                        re.match(re.sub(r"\{[^}]+\}", "[^/]+", stem), r.replace("{", "").replace("}", ""))
                        for r in routes
                    )
                else:
                    concrete = re.sub(r"\{[^}]+\}|<[^>]+>", "X", path)
                    ok = any(p.match(concrete) for p in patterns)
                seen += 1
                if not ok:
                    problems.append(f"{doc.name}: {path}")
    assert seen >= 3
    assert problems == []


# ------------------------------------------------------------------------------------------- paths


TOP = ("ledgerspy", "packaging", "tools", "tests", "config", "data", "frontend", "docs", "archive")


def test_documented_repository_paths_exist():
    problems = []
    for doc in USER_DOCS:
        for seg in code_segments(text(doc)):
            for tok in re.split(r"[\s\"'(),]+", seg):
                tok = tok.replace("\\", "/").rstrip(".:;")
                if not tok.startswith(TOP) or "/" not in tok or re.search(r"[<>…*{}]", tok):
                    continue
                if not (REPO / tok).exists():
                    problems.append(f"{doc.name}: {tok}")
    assert problems == []


def test_documented_bundle_name_matches_the_builder():
    version = re.search(r'^version = "([^"]+)"', text(REPO / "pyproject.toml"), flags=re.M).group(1)
    pattern = r"ledgerspy-[\d.]+-win_amd64-py312-offline\.zip"
    names = {n for d in USER_DOCS for n in re.findall(pattern, text(d))}
    assert names == {f"ledgerspy-{version}-win_amd64-py312-offline.zip"}
    assert "win_amd64-py312-offline" in text(REPO / "packaging" / "build_bundle.py")


# ------------------------------------------------------------------------------------------- UI and reports

# Controls the user guide tells the reader to use; each must exist, verbatim, in the frontend.
UI_LABELS = [
    "Analyse dataset",
    "Confirm mapping and analyse",
    "Run integrity check",
    "Accept all ",
    "✨ Auto-Fix: apply ",
    "Revert",
    "Import bank statement (CSV / XLSX / PDF)",
    "Confirm statement mapping",
    "Continue to the statement mapping",
    "Re-run extraction",
    "Not a transaction",
    "Run reconciliation",
    "Open investigation from this finding",
    "New investigation",
    "Decision on the case",
    "Record decision",
    "Record correction",
    "Add note",
    "Unlink",
    "Evidence manifest",
    "Generate and store",
    "Preview",
    "Entity Similarity Matrix",
    "Relationship Graph",
    "PDF extraction review",
    "Valid next status",
    "LedgerSpy is locked",
]


def test_user_guide_ui_labels_exist_in_the_frontend():
    fe = "\n".join(text(p) for p in (REPO / "frontend").rglob("*") if p.suffix in (".js", ".html"))
    fe += text(REPO / "ledgerspy" / "api" / "app.py")  # the locked page is served by the backend
    guide = text(DOCS / "user-guide.md")
    missing = [lab for lab in UI_LABELS if lab not in fe]
    unused = [lab for lab in UI_LABELS if lab.strip() not in guide]
    assert missing == [] and unused == []


def test_report_types_and_formats_match_the_report_model():
    guide = text(DOCS / "user-guide.md")
    for label in report_model.REPORT_TYPES.values():
        assert label in guide, label
    for fmt in report_model.FORMATS:
        assert fmt.upper() in guide, fmt
    assert "pdf" not in report_model.FORMATS and "docx" not in report_model.FORMATS
    for doc in ("README.md", "docs/user-guide.md", "docs/security.md"):
        assert re.search(r"No (native )?PDF or DOCX", text(REPO / doc)), doc  # missing formats are stated


def test_no_approval_workflow_is_claimed():
    from ledgerspy.api.routes import reports

    assert '"approval_supported": False' in text(Path(reports.__file__))
    for doc in USER_DOCS:
        assert not re.search(r"\b(approve|sign off) (the|a) memo\b", text(doc), flags=re.I), doc.name


# ------------------------------------------------------------------------------------------- hygiene


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda p: p.name)
def test_no_machine_paths_or_tokens_in_documents(doc):
    t = text(doc)
    assert [what for pattern, what in FORBIDDEN_TEXT if pattern.search(t)] == []

"""Pre-P23 gap closure (docs/pre-p23-gap-audit.md): regression tests for the three gaps fixed before P23.

1. Forensic invariants are explicit checks, not `assert` statements, so they also hold under `python -O`.
2. The documented master-data import reaches the workspace the bundled launcher serves.
3. The R6 similarity matrix is served and reads the resolution API only (browser check A15 drives it
   against the API).
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ledgerspy.cli import main
from ledgerspy.errors import InvariantError, LedgerSpyError, require

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------- 1. explicit invariants


def test_runtime_code_has_no_assert_statements():
    """`python -O` removes asserts; a guard on forensic totals must never depend on them."""
    found = []
    for path in sorted((REPO / "ledgerspy").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                found.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert found == []


def test_require_raises_a_refusal_with_its_details():
    require(True, "never raised")
    with pytest.raises(InvariantError) as exc:
        require(False, "rows counted twice", side="bank", counted=3, rows=2)
    assert isinstance(exc.value, LedgerSpyError)
    assert exc.value.code == "internal_invariant_failed" and exc.value.http_status == 500
    assert exc.value.details == {"side": "bank", "counted": 3, "rows": 2}


@pytest.mark.parametrize("optimize", [[], ["-O"]])
def test_a_row_taken_twice_is_refused_even_under_python_O(optimize):
    """The reconciliation engine's 'every row lands in exactly one result' guard, in a fresh interpreter with
    and without -O. The same script shows that -O really strips a plain assert there."""
    script = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from ledgerspy.errors import InvariantError\n"
        "from ledgerspy.reconciliation.engine import _State\n"
        "stripped = True\n"
        "try:\n"
        "    assert False\n"
        "except AssertionError:\n"
        "    stripped = False\n"
        "s = _State.__new__(_State)\n"
        "s.done = set()\n"
        "row = SimpleNamespace(side='bank', txn_id='T-1')\n"
        "s.take([row])\n"
        "try:\n"
        "    s.take([row])\n"
        "    print('accepted', sys.flags.optimize, stripped)\n"
        "except InvariantError as e:\n"
        "    print('refused', sys.flags.optimize, stripped, e.details['txn_id'])\n"
    )
    out = subprocess.run(  # noqa: S603 — this interpreter, fixed script
        [sys.executable, *optimize, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    level = 1 if optimize else 0
    assert out.stdout.split() == ["refused", str(level), str(bool(optimize)), "T-1"]


# ------------------------------------------------------------------------------- 2. master import workflow


def test_the_documented_master_import_targets_the_launchers_workspace():
    """installation.md §4 told users to run the import without --workspace: from the bundle folder that is
    `.\\workspace`, not the `%LOCALAPPDATA%\\LedgerSpy\\workspace` that ledgerspy.cmd serves, so the case was
    not found. The documented command must name the launcher's default workspace."""
    launcher = (REPO / "packaging" / "templates" / "ledgerspy.cmd").read_text(encoding="utf-8")
    default = re.search(r'set "LEDGERSPY_WORKSPACE=([^"]+)"', launcher).group(1)
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    commands = [ln for ln in guide.splitlines() if "entities import-master" in ln and "python" in ln]
    assert commands, "installation.md documents the master import"
    for ln in commands:
        assert f'--workspace "{default}"' in ln, ln
        assert "--case-id" in ln, ln


def test_the_cli_imports_a_master_into_the_named_workspace_only(tmp_path, monkeypatch, capsys):
    """The CLI with --workspace writes to that workspace; without it (from another folder) the case is not
    found and nothing is imported into the served workspace."""
    from ledgerspy.cases import store
    from ledgerspy.cases.db import connect, db_path_for, init_db
    from ledgerspy.config import load_config
    from ledgerspy.resolution.validation import P5_FILES

    served = tmp_path / "served"
    cfg = load_config(overrides={"workspace": {"path": str(served)}})
    init_db(db_path_for(cfg.settings), "t")
    conn = connect(db_path_for(cfg.settings))
    case_id = store.create_case(conn, name="masters", actor="t")["case_id"]
    conn.close()
    fname, mapping = P5_FILES["vendor"]
    master = REPO / "data" / "synthetic" / "reference" / fname
    maps = [a for f, c in mapping.items() for a in ("--map", f"{f}={c}")]
    elsewhere = tmp_path / "bundle"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = ["entities", "import-master", "--case-id", case_id, "--file", str(master)]
    args += ["--entity-type", "vendor"]
    assert main([*args, *maps]) != 0  # no --workspace: `.\workspace` of the current folder, no such case
    assert f"no case {case_id}" in capsys.readouterr().err
    assert main([*args, "--workspace", str(served), *maps]) == 0
    assert main(["entities", "masters", "--case-id", case_id, "--workspace", str(served)]) == 0
    assert '"entity_type": "vendor"' in capsys.readouterr().out


# ------------------------------------------------------------------------------- 3. similarity matrix UI


def test_the_similarity_matrix_is_served_and_reads_only_the_resolution_api():
    page = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="re-sim-card"' in page and '<script src="js/similarity-ui.js"></script>' in page
    js = (REPO / "frontend" / "js" / "similarity-ui.js").read_text(encoding="utf-8")
    calls = re.findall(r"LS_API\.(\w+)\(", js)
    assert set(calls) == {"get", "qs"}  # read-only: no post / put / upload
    paths = re.findall(r"'(/api/[^']*)'", js)
    assert set(paths) == {"/api/datasets/"}  # base() and the dataset (case ID) lookup
    assert "/resolution" in js and "innerHTML" not in js
    assert "never a similarity\n   of zero" in js or "never a similarity of zero" in js.replace("\n   ", " ")

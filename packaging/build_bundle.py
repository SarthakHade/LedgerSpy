"""Build the offline installation bundle (P21).

    python packaging/build_bundle.py --wheelhouse <dir> [--out dist]

Produces `<out>/ledgerspy-<version>-win_amd64-py312-offline.zip`, a folder-style application bundle:

* application files: only git-tracked files from an allow-list (the `ledgerspy` package, the UI, the default
  configuration, documentation, and the synthetic demo data WITHOUT its ground truth);
* `wheelhouse/`: every wheel of `requirements.lock`, each checked against its locked SHA-256;
* `install.cmd`, `ledgerspy.cmd`, `doctor.cmd`, `INSTALL.md`;
* `BUILD-INFO.json` (version, source commit, clean / dirty tree, target) and `MANIFEST.sha256` (every file).

The zip is reproducible: entries are sorted and carry a fixed timestamp, so the same sources and wheels give
the same bytes. The build refuses to include databases, lock files, workspaces, caches, tokens or
user-specific absolute paths. It does not download anything.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)  # zip timestamps: reproducible bytes for identical inputs

INCLUDE = (
    re.compile(r"^ledgerspy/.+\.py$"),
    re.compile(r"^ledgerspy/features/registry_lock\.json$"),
    re.compile(r"^frontend/.+"),
    re.compile(r"^config/default\.yaml$"),
    re.compile(r"^docs/[^/]+\.(md|png)$"),  # not docs/audits/ (historical, quotes a developer's paths)
    re.compile(r"^data/synthetic/reference/[^/]+$"),  # not ground_truth/ (evaluation only, never runtime)
    re.compile(r"^(README\.md|pyproject\.toml|requirements\.lock)$"),
)
FORBIDDEN_NAMES = re.compile(
    r"(\.db|\.sqlite3?|\.lock|\.pyc|\.log|\.env|\.pem|\.key)$|(^|/)(__pycache__|workspace|\.venv|backups|"
    r"evidence|ground_truth)(/|$)",
    re.IGNORECASE,
)
FORBIDDEN_TEXT = (
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s%]+", re.IGNORECASE), "user-specific Windows path"),
    (re.compile(r"(?<![A-Za-z])/(home|Users)/[a-z0-9_.-]+/", re.IGNORECASE), "user-specific Unix path"),
    (re.compile(r"[?&]token=[A-Za-z0-9_-]{20,}"), "session token"),
    (re.compile(r"ledgerspy-session-token\"\s+content=\"[A-Za-z0-9_-]{20,}"), "session token"),
)


class BuildError(RuntimeError):
    pass


def tracked_files() -> list[str]:
    out = subprocess.run(  # noqa: S603 — fixed git command in the repository
        # tracked files plus new, not-ignored ones (never .gitignore'd content: workspaces, dist, caches)
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0") if p)


def source_state() -> dict:
    def git(*a: str) -> str:
        return subprocess.run(  # noqa: S603 — fixed git command in the repository
            ["git", *a],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,  # noqa: S607
        ).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def locked() -> dict[str, str]:
    """{wheel package name (normalised): sha256} from requirements.lock."""
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pairs = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})", text, re.M)
    return {re.sub(r"[-_.]+", "-", n).lower(): (v, h) for n, v, h in pairs}


def collect(wheelhouse: Path | None) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for rel in tracked_files():
        if any(p.match(rel) for p in INCLUDE):
            path = ROOT / rel
            if path.is_file():
                files[rel] = path.read_bytes()
    for t in sorted(TEMPLATES.iterdir()):
        files[t.name] = t.read_bytes()
    files["INSTALL.md"] = (ROOT / "docs" / "installation.md").read_bytes()
    if wheelhouse is not None:
        want = locked()
        seen = set()
        for whl in sorted(wheelhouse.glob("*.whl")):
            name, version = whl.name.split("-")[:2]
            key = re.sub(r"[-_.]+", "-", name).lower()
            if key not in want:
                continue  # wheels not in the lock are never shipped
            data = whl.read_bytes()
            if (version, hashlib.sha256(data).hexdigest()) != want[key]:
                raise BuildError(f"{whl.name} does not match requirements.lock (version or SHA-256)")
            files[f"wheelhouse/{whl.name}"] = data
            seen.add(key)
        missing = sorted(set(want) - seen)
        if missing:
            raise BuildError(f"wheelhouse lacks locked packages: {missing}")
    return files


def scan(files: dict[str, bytes]) -> None:
    problems = []
    for rel, data in files.items():
        if FORBIDDEN_NAMES.search(rel) and rel != "requirements.lock":
            problems.append(f"{rel}: forbidden file")
        if rel.endswith((".whl", ".png")):
            continue
        text = data.decode("utf-8", errors="replace")
        for pattern, what in FORBIDDEN_TEXT:
            if pattern.search(text):
                problems.append(f"{rel}: contains a {what}")
    if problems:
        raise BuildError("refusing to build:\n  " + "\n  ".join(problems))


def build(wheelhouse: Path | None, out: Path) -> tuple[Path, dict]:
    files = collect(wheelhouse)
    scan(files)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    info = {
        "name": "ledgerspy",
        "version": version,
        "source": source_state(),
        "target": "Windows x64, CPython 3.12 (cp312-win_amd64 wheels)",
        "wheelhouse": wheelhouse is not None,
        "files": len(files) + 2,
        "note": "Folder bundle: requires a local 64-bit CPython 3.12 and installs only from wheelhouse/. "
        "It is not a standalone executable.",
    }
    files["BUILD-INFO.json"] = (json.dumps(info, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest = "".join(f"{hashlib.sha256(files[r]).hexdigest()}  {r}\n" for r in sorted(files))
    files["MANIFEST.sha256"] = manifest.encode("utf-8")
    top = f"ledgerspy-{version}"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"ledgerspy-{version}-win_amd64-py312-offline{'' if wheelhouse else '-nowheels'}.zip"
    tmp = target.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(files):
            zi = zipfile.ZipInfo(f"{top}/{rel}", date_time=FIXED_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, files[rel])
    tmp.replace(target)
    return target, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the LedgerSpy offline bundle (no downloads).")
    ap.add_argument("--wheelhouse", type=Path, help="folder with the wheels of requirements.lock")
    ap.add_argument("--no-wheels", action="store_true", help="build without wheelhouse/ (for tests only)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    args = ap.parse_args(argv)
    if not args.wheelhouse and not args.no_wheels:
        ap.error("--wheelhouse is required (or --no-wheels for a test build)")
    try:
        target, info = build(None if args.no_wheels else args.wheelhouse, args.out)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"built {target} ({target.stat().st_size} bytes, {info['files']} files)\nsha256 {digest}")
    if info["source"]["dirty"]:
        print("NOTE: built from a working tree with uncommitted changes (recorded in BUILD-INFO.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

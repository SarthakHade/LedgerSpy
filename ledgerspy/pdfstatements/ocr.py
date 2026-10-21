"""Local OCR through the Tesseract executable (no network, no cloud service, no Python OCR wrapper).

Discovery checks each `pdf.ocr.executable_candidates` entry in order (a name on PATH or an absolute path),
runs `--version` and `--list-langs`, and reports exactly why OCR is unavailable when it is: no executable, a
failing executable, or missing language data. Nothing falls back to an empty "successful" result.

Recognition runs `tesseract <png> stdout -l <langs> --psm <psm> --dpi <dpi> tsv` with OMP_THREAD_LIMIT=1 (same
input, version and settings → same output) and a per-page timeout. Word boxes are converted from image pixels
to PDF points (top-left origin). Word confidence is Tesseract's 0–100 recognition confidence: a statement
about glyph recognition, never about the financial correctness of a value.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OcrEngine:
    available: bool
    reason: str | None = None
    executable: str | None = None
    version: str | None = None
    languages: list[str] = field(default_factory=list)  # requested languages
    installed_languages: list[str] = field(default_factory=list)

    def provenance(self, ocr_cfg: dict[str, Any]) -> dict[str, Any]:
        return {
            "engine": "tesseract",
            "available": self.available,
            "reason": self.reason,
            "version": self.version,
            "languages": self.languages,
            "psm": ocr_cfg["psm"],
            "dpi": ocr_cfg["dpi"],
            "executable": None if self.executable is None else Path(self.executable).name,
        }


WORKER_TEMP_ENV = "LEDGERSPY_PDF_WORKER_TEMP_DIR"  # = ledgerspy.storage_limits.WORKER_TEMP_ENV


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME")}
    env["OMP_THREAD_LIMIT"] = "1"
    # Set only by a host's worker policy (the hosted demo): Tesseract and its children get that folder for
    # every temporary file. Offline the variable is absent and nothing changes.
    work = os.environ.get(WORKER_TEMP_ENV, "")
    if work and os.path.isabs(work) and os.path.isdir(work):
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = work
    return env


def _resolve(candidate: str) -> str | None:
    if os.path.isabs(candidate) or "/" in candidate or "\\" in candidate:
        return candidate if Path(candidate).is_file() else None
    return shutil.which(candidate)


def discover(ocr_cfg: dict[str, Any]) -> OcrEngine:
    langs = list(ocr_cfg["languages"])
    tried = []
    for cand in ocr_cfg["executable_candidates"]:
        exe = _resolve(cand)
        if exe is None:
            tried.append(cand)
            continue
        try:
            v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, env=_env())  # noqa: S603
            ls = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30, env=_env())  # noqa: S603
        except (OSError, subprocess.SubprocessError) as exc:
            return OcrEngine(False, f"Tesseract at {exe} could not be run: {exc}", exe, languages=langs)
        first = (v.stdout or v.stderr).strip().splitlines()
        version = first[0].replace("tesseract", "").strip() if first else None
        installed = sorted(
            {
                x.strip()
                for x in (ls.stdout + "\n" + ls.stderr).splitlines()[1:]
                if x.strip() and " " not in x.strip()
            }
        )
        if v.returncode != 0 or not version:
            return OcrEngine(False, f"Tesseract at {exe} did not report a version", exe, languages=langs)
        missing = [x for x in langs if x not in installed]
        if missing:
            return OcrEngine(
                False,
                f"Tesseract {version} has no language data for {missing} (installed: {installed}); install "
                "the traineddata files into its tessdata folder",
                exe,
                version,
                langs,
                installed,
            )
        return OcrEngine(True, None, exe, version, langs, installed)
    return OcrEngine(
        False,
        "no Tesseract executable found (tried: " + ", ".join(tried) + "); install Tesseract 5 locally or set "
        "pdf.ocr.executable_candidates",
        languages=langs,
    )


class OcrError(RuntimeError):
    pass


def recognise(engine: OcrEngine, png: Path, ocr_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Words of one rendered page image: {text, x0, top, x1, bottom (PDF points), conf (0–100)}."""
    cmd = [
        engine.executable,
        str(png),
        "stdout",
        "-l",
        "+".join(engine.languages),
        "--psm",
        str(ocr_cfg["psm"]),
        "--dpi",
        str(ocr_cfg["dpi"]),
        "tsv",
    ]
    try:
        r = subprocess.run(  # noqa: S603 — fixed argument list, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=ocr_cfg["page_timeout_seconds"],
            env=_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"OCR timed out after {ocr_cfg['page_timeout_seconds']} s") from exc
    if r.returncode != 0:
        raise OcrError(f"Tesseract failed (exit {r.returncode}): {r.stderr.strip()[:300]}")
    scale = 72.0 / ocr_cfg["dpi"]
    words = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 12 or parts[0] != "5" or not parts[11].strip():
            continue
        left, top, width, height = (int(parts[i]) for i in (6, 7, 8, 9))
        words.append(
            {
                "text": parts[11].strip(),
                "x0": round(left * scale, 2),
                "top": round(top * scale, 2),
                "x1": round((left + width) * scale, 2),
                "bottom": round((top + height) * scale, 2),
                "conf": round(float(parts[10]), 2),
            }
        )
    return words

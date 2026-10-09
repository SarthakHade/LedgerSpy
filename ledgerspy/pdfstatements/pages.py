"""PDF access through PDFium (pypdfium2): validation, embedded-text words and boxes, image coverage, render.

Coordinates are PDF points with a TOP-LEFT origin: x0, top, x1, bottom (PDFium char boxes are bottom-left
based and are flipped with the page height). Words are runs of non-space characters, split also at horizontal
gaps wider than the character height (text placed without explicit spaces). Nothing here modifies the PDF; the
document is opened read-only from the evidence path.
"""

from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PDF_MAGIC = b"%PDF-"
REPLACEMENT = "�"


class PdfError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def versions() -> dict[str, str]:
    return {"pypdfium2": str(pdfium.V_PYPDFIUM2), "pdfium": str(pdfium.V_LIBPDFIUM)}


def open_document(path: Path) -> pdfium.PdfDocument:
    with path.open("rb") as f:
        head = f.read(1024)
    if PDF_MAGIC not in head:
        raise PdfError("pdf_malformed", "The file is not a PDF (no %PDF- header).")
    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise PdfError(
                "pdf_encrypted",
                "The PDF is password-protected. Encrypted statements are not opened: export an unprotected "
                "copy from the bank portal and upload that.",
            ) from exc
        raise PdfError("pdf_malformed", f"The PDF cannot be read: {msg}") from exc


def has_encrypt_dictionary(path: Path) -> bool:
    """True when the file declares encryption (an owner-password-only PDF still opens without a password)."""
    data = path.read_bytes()
    return b"/Encrypt" in data[-65536:] or b"/Encrypt" in data[:4096]


def page_size(page: pdfium.PdfPage) -> tuple[float, float]:
    w, h = page.get_size()
    return float(w), float(h)


def text_words(page: pdfium.PdfPage) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Embedded-text words: {text, x0, top, x1, bottom, conf: None}; plus character statistics."""
    _, h = page_size(page)
    tp = page.get_textpage()
    try:
        n = tp.count_chars()
        words: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        stats = {"chars": 0, "replacement_chars": 0}
        for i in range(n):
            ch = tp.get_text_range(i, 1)
            if not ch or ch.isspace() or ch in "\x00\x02":
                cur = None
                continue
            left, bottom, right, top = tp.get_charbox(i)
            box = {"x0": left, "top": h - top, "x1": right, "bottom": h - bottom}
            stats["chars"] += 1
            if ch == REPLACEMENT:
                stats["replacement_chars"] += 1
            if cur is not None:
                # punctuation boxes are tiny: compare with the larger of the word's and the character's height
                ref = max(cur["bottom"] - cur["top"], box["bottom"] - box["top"], 1.0)
                centre = (box["top"] + box["bottom"]) / 2
                if box["x0"] - cur["x1"] > 0.6 * ref or not (
                    cur["top"] - 0.4 * ref <= centre <= cur["bottom"] + 0.4 * ref
                ):
                    cur = None
            if cur is None:
                cur = {"text": ch, **box, "conf": None}
                words.append(cur)
            else:
                cur["text"] += ch
                cur["x1"] = max(cur["x1"], box["x1"])
                cur["top"] = min(cur["top"], box["top"])
                cur["bottom"] = max(cur["bottom"], box["bottom"])
        for w in words:
            for k in ("x0", "top", "x1", "bottom"):
                w[k] = round(w[k], 2)
        return words, stats
    finally:
        tp.close()


def image_coverage(page: pdfium.PdfPage) -> float:
    """Largest share of the page area covered by one image object (0 when there is none)."""
    w, h = page_size(page)
    area = max(w * h, 1.0)
    best = 0.0
    for obj in page.get_objects(max_depth=2):
        if obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
            continue
        left, bottom, right, top = obj.get_pos()
        best = max(best, max(0.0, right - left) * max(0.0, top - bottom) / area)
    return round(min(best, 1.0), 4)


def render(page: pdfium.PdfPage, dpi: int, max_pixels: int):
    """Grayscale PIL image at `dpi`. Refused (never downscaled) above `max_pixels`."""
    w, h = page_size(page)
    px = (w * dpi / 72) * (h * dpi / 72)
    if px > max_pixels:
        raise PdfError(
            "page_too_large",
            f"rendering this page at {dpi} dpi needs {int(px)} pixels (limit {max_pixels})",
            {"pixels": int(px), "limit": max_pixels},
        )
    return page.render(scale=dpi / 72).to_pil().convert("L")

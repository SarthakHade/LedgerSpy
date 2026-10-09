"""Subprocess entry point: `python -m ledgerspy.pdfstatements.worker` (JSON request on stdin, JSON on stdout).

PDF parsing (PDFium, native code) and OCR run here, never in the server process: a hostile or pathological PDF
can at worst crash or stall this child, which the parent kills after `pdf.worker_timeout_seconds` and reports
as a failed extraction. The worker only reads the given evidence path and writes nothing except temporary OCR
images (deleted) and, for `render`, the requested PNG under the given output path.

Modes: `extract` → extraction result; `render` → one page as PNG (reviewer preview); `ocr_status` → engine.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    req = json.loads(sys.stdin.read())
    from ledgerspy.pdfstatements import extract, ocr, pages

    try:
        if req["mode"] == "extract":
            out = extract.run(Path(req["path"]), req["settings"])
        elif req["mode"] == "render":
            doc = pages.open_document(Path(req["path"]))
            try:
                if not 1 <= req["page"] <= len(doc):
                    raise pages.PdfError("page_not_found", f"page {req['page']} does not exist")
                page = doc[req["page"] - 1]
                img = pages.render(page, req["dpi"], req["max_pixels"])
                img.save(req["out"], format="PNG")
                out = {"width": img.width, "height": img.height}
            finally:
                doc.close()
        elif req["mode"] == "ocr_status":
            out = ocr.discover(req["ocr"]).provenance(req["ocr"])
        else:
            out = {"error": {"code": "bad_request", "message": "unknown mode"}}
    except pages.PdfError as exc:
        out = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

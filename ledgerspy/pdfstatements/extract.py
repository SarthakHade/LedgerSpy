"""Extraction of one PDF statement: pages → words (embedded text or OCR) → layout items → review flags.

Per page the method is decided from the page itself, never assumed:
- `text`: at least `pdf.min_text_chars` non-space characters of embedded text;
- `text_over_image`: the same, but one image covers ≥ `pdf.image_page_min_coverage` of the page (a scan with a
  text layer someone else produced — its accuracy is unknown, so its rows are review-required);
- `ocr`: (almost) no embedded text and a page-sized image — rendered at `pdf.ocr.dpi` and recognised locally;
- `not_processed`: OCR was needed but is unavailable, the page is too large to render, or OCR failed — the
  extraction is then `incomplete` and cannot be confirmed;
- `empty`: neither text nor an image.

The result is plain JSON (produced in a subprocess, see worker.py). Item order is page, then line order.
"""

import tempfile
import time
from pathlib import Path
from typing import Any

from ledgerspy.pdfstatements import layout, ocr, pages, validate

BODY_ROLES = (
    "date",
    "value_date",
    "description",
    "reference",
    "debit",
    "credit",
    "amount",
    "drcr",
    "balance",
)


def _role_fields(it: dict[str, Any], cols: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    others: dict[str, str] = {}
    for i, c in it["cells"].items():
        col = cols[i]
        if col["role"] == "other":
            others[col["literal"]] = c["text"]
        else:
            fields[col["role"]] = c["text"]
    return {"fields": fields, "others": others}


def run(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    pcfg = settings["pdf"]
    formats = settings["date_formats"]
    t_start = time.perf_counter()
    doc = pages.open_document(path)
    try:
        n_pages = len(doc)
        if n_pages > pcfg["max_pages"]:
            raise pages.PdfError(
                "pdf_too_many_pages",
                f"The PDF has {n_pages} pages; the limit is {pcfg['max_pages']} (pdf.max_pages).",
                {"pages": n_pages, "limit": pcfg["max_pages"]},
            )
        if n_pages == 0:
            raise pages.PdfError("pdf_no_pages", "The PDF has no pages.")
        engine = None
        page_reports: list[dict[str, Any]] = []
        raw_items: list[dict[str, Any]] = []
        carried = None
        seen_txn = False
        context: dict[str, Any] = {"account": None, "currency": None}
        stated: dict[str, Any] = {}
        warnings: list[str] = []
        for pno in range(n_pages):
            t0 = time.perf_counter()
            page = doc[pno]
            try:
                w, h = pages.page_size(page)
                words, stats = pages.text_words(page)
                cover = pages.image_coverage(page)
                rep: dict[str, Any] = {
                    "page": pno + 1,
                    "width": w,
                    "height": h,
                    "text_chars": stats["chars"],
                    "replacement_chars": stats["replacement_chars"],
                    "image_coverage": cover,
                    "warnings": [],
                }
                scanned = cover >= pcfg["image_page_min_coverage"]
                if stats["chars"] >= pcfg["min_text_chars"] or (stats["chars"] and not scanned):
                    method = "text_over_image" if scanned else "text"
                elif scanned:
                    if engine is None:
                        engine = ocr.discover(pcfg["ocr"])
                    if not engine.available:
                        method, words = "not_processed", []
                        rep["reason"] = "OCR needed but unavailable: " + engine.reason
                    else:
                        try:
                            img = pages.render(page, pcfg["ocr"]["dpi"], pcfg["render"]["max_pixels"])
                            with tempfile.TemporaryDirectory(prefix="ls-ocr-") as tmp:
                                png = Path(tmp) / f"p{pno + 1}.png"
                                img.save(png, dpi=(pcfg["ocr"]["dpi"], pcfg["ocr"]["dpi"]))
                                words = ocr.recognise(engine, png, pcfg["ocr"])
                            method = "ocr"
                            confs = [x["conf"] for x in words]
                            rep["ocr_words"] = len(words)
                            rep["ocr_mean_confidence"] = round(sum(confs) / len(confs), 2) if confs else None
                        except (pages.PdfError, ocr.OcrError) as exc:
                            method, words = "not_processed", []
                            rep["reason"] = getattr(exc, "message", str(exc))
                else:
                    method, words = "empty", []
                rep["method"] = method
                if method == "text_over_image":
                    rep["warnings"].append(
                        "embedded text over a page-sized image (possibly a prior OCR layer): accuracy unknown"
                    )
                lines = layout.lines_of(words, pcfg["layout"]["line_tolerance"])
                res = layout.classify_page(lines, carried, pcfg["layout"], formats, seen_txn)
                seen_txn = res["seen_transaction"]
                if res["columns"] is not None:
                    carried = res["columns"]
                rep["warnings"] += res["warnings"]
                if (
                    lines
                    and method in ("text", "ocr", "text_over_image")
                    and not any(it["kind"] == "table_header" for it in res["items"])
                    and not any("columns_source" in it for it in res["items"])
                ):
                    rep["warnings"].append("layout_not_recognised: no transaction table header on this page")
                for ln_no, it in enumerate(res["items"], start=1):
                    it["page"], it["line_no"], it["method"] = pno + 1, ln_no, method
                    raw_items.append(it)
                rep["lines"] = len(lines)
                rep["seconds"] = round(time.perf_counter() - t0, 3)
                page_reports.append(rep)
            finally:
                page.close()
    finally:
        doc.close()

    # ---- items: fields, statement context, continuations, review
    items: list[dict[str, Any]] = []
    last_txn = None
    header_cols: list[dict[str, Any]] = []
    for it in raw_items:
        ln = it["line"]
        out: dict[str, Any] = {
            "item_no": len(items) + 1,
            "page": it["page"],
            "line_no": it["line_no"],
            "kind": it["kind"],
            "method": it["method"],
            "bbox": ln["bbox"],
            "text": ln["text"],
        }
        if it["kind"] == "table_header":
            header_cols = it["columns"]
            out["columns"] = [
                {k: c[k] for k in ("role", "literal", "currency")}
                | {"x0": round(c["x0"], 2), "x1": round(c["x1"], 2)}
                for c in header_cols
            ]
            for c in header_cols:
                if c["currency"]:
                    context["currency"] = context["currency"] or c["currency"]
        info = it.get("info") or {}
        if info:
            out["info"] = info
            if info.get("account"):
                context["account"] = info["account"]
            if info.get("currency"):
                context["currency"] = info["currency"]
            for k in ("opening_balance", "closing_balance", "period"):
                if k in info:
                    stated.setdefault(
                        k, info[k] | {"item_no": out["item_no"]} if isinstance(info[k], dict) else info[k]
                    )
        if "cells" in it:
            cols = it.get("columns_used") or header_cols
            rf = _role_fields(it, cols) if cols else {"fields": {}, "others": {}}
            out["fields"], out["other_columns"] = rf["fields"], rf["others"]
            out["cells"] = {
                cols[i]["role"] if cols[i]["role"] != "other" else "other:" + cols[i]["literal"]: {
                    "text": c["text"],
                    "bbox": [round(v, 2) for v in c["bbox"]],
                    "min_ocr_confidence": c["min_conf"],
                }
                for i, c in it["cells"].items()
            }
            out["uncertain_words"] = it.get("uncertain_words", [])
            out["columns_source"] = it.get("columns_source")
            out["roles_present"] = sorted({c["role"] for c in cols if c["role"] != "other"})
        if it["kind"] in ("opening_balance", "closing_balance", "carry_forward") and "fields" in out:
            bal = out["fields"].get("balance") or (out["fields"].get("amount"))
            if it["kind"] in ("opening_balance", "closing_balance") and bal:
                stated.setdefault(it["kind"], {"value": bal, "indicator": None, "item_no": out["item_no"]})
        if it["kind"] == "transaction":
            out["context"] = {"account": context["account"], "currency": context["currency"]}
            out["ocr_words"] = [
                {"text": w["text"], "conf": w["conf"]} for w in ln["words"] if w["conf"] is not None
            ]
            out["replacement_chars"] = ln["text"].count(pages.REPLACEMENT)
            out["continuation_items"] = []
            last_txn = out
        elif it["kind"] == "continuation" and last_txn is not None:
            out["continues_item"] = last_txn["item_no"]
            last_txn["continuation_items"].append(out["item_no"])
            d = out["fields"].get("description")
            if d:
                prev = last_txn["fields"].get("description")
                last_txn["fields"]["description"] = f"{prev} {d}" if prev else d
            last_txn["text"] += "\n" + out["text"]
            last_txn["ocr_words"] += [
                {"text": w["text"], "conf": w["conf"]} for w in ln["words"] if w["conf"] is not None
            ]
            last_txn["uncertain_words"] += out.get("uncertain_words", [])
        elif it["kind"] not in ("continuation",):
            last_txn = None if it["kind"] not in ("page_footer", "page_header") else last_txn
        if it["kind"] == "unparsed":
            out["reason"] = it.get("reason")
        items.append(out)

    txns = [x for x in items if x["kind"] == "transaction"]
    # The separator is a property of the statement: decided on embedded-text rows when there are any (exact
    # glyphs), otherwise on all rows. Cells that do not parse with it are flagged on their own rows.
    text_rows = [t for t in txns if t["method"] == "text"]
    sep = validate.decimal_separator(text_rows or txns, settings["min_parse_rate"])
    for t in txns:
        validate.review_row(
            t, formats, sep, pcfg["ocr"]["min_word_confidence"], pcfg["auto_accept_text_rows"]
        )
    for x in items:  # unparsed lines inside a table must be acknowledged: they may be missed transactions
        if x["kind"] == "unparsed":
            x["review_required"] = True
            x["reasons"] = ["unparsed_line_in_table"]
        elif x["kind"] != "transaction":
            x["review_required"] = False
            x["reasons"] = []
    debit_literals = {
        layout.key(layout._CUR_IN_HEADER.sub("", c["literal"]))
        for x in items
        if x["kind"] == "table_header"
        for c in x["columns"]
        if c["role"] == "debit"
    }
    credit_literals = {
        layout.key(layout._CUR_IN_HEADER.sub("", c["literal"]))
        for x in items
        if x["kind"] == "table_header"
        for c in x["columns"]
        if c["role"] == "credit"
    }
    header_semantics = None
    if (
        debit_literals
        and debit_literals <= layout.OUTFLOW_LITERALS
        and credit_literals <= layout.INFLOW_LITERALS
    ):
        header_semantics = "outflow"
    checks = validate.statement_checks(txns, stated, header_semantics, sep)
    not_processed = [p["page"] for p in page_reports if p["method"] == "not_processed"]
    unrecognised = [
        p["page"] for p in page_reports if any(w.startswith("layout_not_recognised") for w in p["warnings"])
    ]
    if unrecognised:
        warnings.append(
            f"no transaction table recognised on page(s) {unrecognised}: those pages yield no rows"
        )
    for t in txns:
        confs = [w["conf"] for w in t.pop("ocr_words", [])]
        t["ocr_min_confidence"] = min(confs) if confs else None
    kinds: dict[str, int] = {}
    for x in items:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    classified = sum(v for k, v in kinds.items() if k != "unparsed")
    return {
        "status": "incomplete" if not_processed else "completed",
        "pages": page_reports,
        "not_processed_pages": not_processed,
        "items": items,
        "statement": {
            "stated": stated,
            "header_semantics": header_semantics,
            "decimal_separator": sep,
            "checks": checks,
        },
        "coverage": {
            "lines": len(items),
            "by_kind": dict(sorted(kinds.items())),
            "classified_share": round(classified / len(items), 6) if items else None,
            "transactions": len(txns),
            "review_required": sum(1 for x in items if x.get("review_required")),
        },
        "warnings": warnings,
        "engine": {
            "pdf": pages.versions(),
            "ocr": engine.provenance(pcfg["ocr"])
            if engine
            else {"engine": "tesseract", "used": False, "reason": "no page needed OCR"},
        },
        "seconds": round(time.perf_counter() - t_start, 3),
    }

"""Safe, streaming readers for CSV and XLSX.

A reader is opened once with detection (encoding, delimiter, sheet) at upload time. The detected settings are
stored with the dataset, and later reads reopen the evidence copy with exactly those settings. Every read
re-applies the limits from config (`ingest`), streams records one at a time and never evaluates content.

Row numbering: `source_row` is the 1-based ordinal of the record after the header, counting blank records,
so it matches what a person sees in a spreadsheet (spreadsheet row = header row + source_row). Blank
records are skipped (they produce no transaction) but keep their number.
"""

import codecs
import csv
import io
import re
import warnings
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ledgerspy.config.schema import IngestConfig
from ledgerspy.ingest.errors import IngestError
from ledgerspy.ingest.values import Cell, is_blank

Format = Literal["csv", "xlsx"]

CSV_EXTENSIONS = {".csv", ".txt", ".tsv"}
XLSX_EXTENSIONS = {".xlsx"}
REFUSED_WORKBOOKS = {".xlsm", ".xlsb", ".xls", ".xltm", ".xltx", ".xlam", ".ods"}

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls and password-encrypted .xlsx
_CHUNK = 1024 * 1024


@dataclass
class TableSource:
    path: Path
    format: Format
    details: dict[str, Any]
    header: list[str]
    stats: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- format


def detect_format(path: Path, filename: str) -> Format:
    ext = Path(filename).suffix.lower()
    with path.open("rb") as f:
        head = f.read(8)
    if not head:
        raise IngestError("empty_file", "The file is empty.")
    if ext in REFUSED_WORKBOOKS:
        raise IngestError(
            "unsupported_format",
            f"{ext} workbooks are not accepted (macro-enabled, legacy or non-OOXML). Save as .xlsx or .csv.",
            http_status=415,
        )
    if ext in XLSX_EXTENSIONS:
        if head.startswith(_OLE_MAGIC):
            raise IngestError(
                "xlsx_encrypted_or_legacy",
                "The .xlsx file is password-protected or actually a legacy .xls. "
                "Remove protection or re-save "
                "as .xlsx.",
            )
        if not head.startswith(_ZIP_MAGIC):
            raise IngestError("xlsx_not_zip", "The .xlsx file is not a valid Office Open XML (zip) workbook.")
        return "xlsx"
    if ext in CSV_EXTENSIONS:
        if head.startswith(_ZIP_MAGIC) or head.startswith(_OLE_MAGIC):
            raise IngestError(
                "extension_mismatch", "The file has a text extension but contains a binary workbook."
            )
        return "csv"
    if ext == ".pdf":
        raise IngestError(
            "unsupported_format",
            "PDF statements (text or scanned / OCR) are not supported yet: they come in roadmap P14b. Export "
            "the statement as .csv or .xlsx.",
            {"deferred_to": "P14b"},
            http_status=415,
        )
    raise IngestError(
        "unsupported_format",
        f"Unsupported file type {ext or '(none)'!r}; upload .csv or .xlsx.",
        http_status=415,
    )


# ----------------------------------------------------------------------------------------------- header


def _resolve_header(cells: list[Cell], max_columns: int) -> list[str]:
    names = ["" if is_blank(c) else str(c).strip() for c in cells]
    while names and names[-1] == "":
        names.pop()
    if len(names) < 2:
        raise IngestError(
            "too_few_columns", "The header row has fewer than two columns; a ledger needs more."
        )
    if len(names) > max_columns:
        raise IngestError(
            "too_many_columns",
            f"The header has {len(names)} columns; the limit is {max_columns}.",
            {"columns": len(names), "limit": max_columns},
        )
    resolved = [n if n else f"(unnamed column {i + 1})" for i, n in enumerate(names)]
    seen: dict[str, int] = {}
    for i, n in enumerate(resolved):
        if n in seen:
            raise IngestError(
                "duplicate_header",
                f"Column name {n!r} appears more than once (columns {seen[n] + 1} and {i + 1}).",
                {"column": n},
            )
        seen[n] = i
    return resolved


def _shape(cells: list[Cell], width: int, source_row: int, where: str) -> list[Cell] | None:
    """Pad/trim a record to the header width. None = blank record. Extra non-blank cells = malformed."""
    if all(is_blank(c) for c in cells):
        return None
    if len(cells) > width:
        if any(not is_blank(c) for c in cells[width:]):
            raise IngestError(
                "ragged_row",
                f"Data row {source_row} ({where}) has {len(cells)} fields but the header has {width}.",
                {"source_row": source_row, "fields": len(cells), "header_fields": width},
            )
        cells = cells[:width]
    elif len(cells) < width:
        raise IngestError(
            "ragged_row",
            f"Data row {source_row} ({where}) has {len(cells)} fields but the header has {width}.",
            {"source_row": source_row, "fields": len(cells), "header_fields": width},
        )
    return cells


# ----------------------------------------------------------------------------------------------- CSV

_BOMS = [
    (codecs.BOM_UTF8, "utf-8-sig", "utf-8 (BOM)"),
    (codecs.BOM_UTF16_LE, "utf-16", "utf-16-le (BOM)"),
    (codecs.BOM_UTF16_BE, "utf-16", "utf-16-be (BOM)"),
]


def _decodes_cleanly(path: Path, codec: str, check_nul: bool) -> bool:
    decoder = codecs.getincrementaldecoder(codec)(errors="strict")
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            if check_nul and b"\x00" in chunk:
                raise IngestError("binary_content", "The file contains NUL bytes; it is not a text CSV.")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                return False
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return False
    return True


def _detect_encoding(path: Path, cfg: IngestConfig) -> tuple[str, str]:
    with path.open("rb") as f:
        head = f.read(4)
    for bom, codec, label in _BOMS:
        if head.startswith(bom):
            if not _decodes_cleanly(path, codec, check_nul=False):
                raise IngestError(
                    "encoding_unsupported", f"The file starts with a {label} marker but does not decode."
                )
            return codec, label
    for encoding in cfg.csv.encodings:
        if _decodes_cleanly(path, encoding, check_nul=True):
            return encoding, encoding
    raise IngestError(
        "encoding_unsupported",
        f"The file is not valid text in any accepted encoding ({', '.join(cfg.csv.encodings)}). "
        "Re-save it as UTF-8 CSV.",
    )


def _sample_records(text: str, delimiter: str, truncated: bool) -> list[list[str]]:
    records: list[list[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    try:
        for rec in reader:
            records.append(rec)
    except csv.Error:
        # Malformed (or cut inside a quoted field): keep the records parsed so far; the failing one was not
        # appended. The full parse reports the precise error later.
        truncated = False
    if truncated and records:
        records.pop()  # the sample was cut by size: its last record may be partial
    return [r for r in records if any(c.strip() for c in r)]


def _detect_delimiter(path: Path, codec: str, cfg: IngestConfig) -> str:
    """Pick the delimiter that splits the header into >= 2 columns and agrees with most sampled records.

    Agreement needs only a majority, so a malformed record later in the file is reported by the full parse
    (with its line number) rather than masquerading as "no delimiter".
    """
    with path.open(encoding=codec, newline="") as f:
        sample = f.read(cfg.csv.sniff_bytes)
        truncated = bool(f.read(1))
    if not sample.strip():
        raise IngestError("no_header", "The file has no header row (it is empty or blank).")
    scored: dict[str, tuple[float, int]] = {}
    for d in cfg.csv.delimiters:
        records = _sample_records(sample, d, truncated)
        if not records or len(records[0]) < 2:
            continue
        width, data = len(records[0]), records[1:]
        agreement = sum(1 for r in data if len(r) == width) / len(data) if data else 1.0
        if agreement >= 0.5:
            scored[d] = (agreement, width)
    if not scored:
        raise IngestError(
            "delimiter_not_detected",
            "Could not find a delimiter ("
            + " ".join(repr(d) for d in cfg.csv.delimiters)
            + ") that splits the "
            "header into at least two columns consistently with the data. The file may have a single column.",
        )
    perfect = [d for d, (agreement, _) in scored.items() if agreement == 1.0]
    if len(perfect) > 1:
        raise IngestError(
            "delimiter_ambiguous",
            "More than one delimiter fits this file: "
            + ", ".join(f"{d!r} ({scored[d][1]} columns)" for d in perfect)
            + ". Upload again with an explicit delimiter.",
            {"candidates": perfect},
        )
    return max(scored, key=lambda d: scored[d])


def open_csv(path: Path, cfg: IngestConfig, delimiter: str | None = None) -> TableSource:
    codec, label = _detect_encoding(path, cfg)
    if delimiter is not None and delimiter not in cfg.csv.delimiters:
        raise IngestError(
            "delimiter_unsupported", f"Delimiter {delimiter!r} is not one of the accepted delimiters."
        )
    delimiter = delimiter or _detect_delimiter(path, codec, cfg)
    details = {"codec": codec, "encoding": label, "delimiter": delimiter, "quotechar": '"', "header_row": 1}
    return _with_header(TableSource(path, "csv", details, []), cfg)


def _csv_rows(src: TableSource, cfg: IngestConfig) -> Iterator[tuple[int, list[Cell]]]:
    """Yields (physical line, record) pairs; the first non-blank record is the header."""
    csv.field_size_limit(cfg.max_cell_chars)
    with src.path.open(encoding=src.details["codec"], newline="") as f:
        reader = csv.reader(
            f, delimiter=src.details["delimiter"], quotechar=src.details["quotechar"], strict=True
        )
        try:
            for rec in reader:
                yield reader.line_num, rec
        except csv.Error as exc:
            message = str(exc)
            if "field larger than field limit" in message:
                raise IngestError(
                    "cell_too_large",
                    f"A cell near line {reader.line_num} exceeds {cfg.max_cell_chars} characters.",
                    {"line": reader.line_num},
                ) from exc
            raise IngestError(
                "malformed_csv",
                f"Malformed CSV near line {reader.line_num}: {message}.",
                {"line": reader.line_num},
            ) from exc


# ----------------------------------------------------------------------------------------------- XLSX

_DTD = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)


def _inspect_xlsx_zip(path: Path, cfg: IngestConfig) -> None:
    x = cfg.xlsx
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise IngestError("xlsx_invalid", "The .xlsx file is not a readable zip archive.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > x.max_zip_entries:
            raise IngestError(
                "xlsx_too_many_entries", f"The workbook has {len(infos)} parts (limit {x.max_zip_entries})."
            )
        total = sum(i.file_size for i in infos)
        if total > x.max_uncompressed_bytes:
            raise IngestError(
                "xlsx_zip_bomb",
                f"The workbook expands to {total} bytes (limit {x.max_uncompressed_bytes}).",
                {"uncompressed_bytes": total},
            )
        for i in infos:
            ratio = i.file_size / max(i.compress_size, 1)
            if i.file_size >= x.ratio_check_min_bytes and ratio > x.max_compression_ratio:
                raise IngestError(
                    "xlsx_zip_bomb",
                    f"Workbook part {i.filename!r} has compression ratio {ratio:.0f}:1 "
                    f"(limit {x.max_compression_ratio}:1).",
                    {"part": i.filename, "ratio": round(ratio)},
                )
        names = {i.filename.lower(): i.filename for i in infos}
        if "xl/workbook.xml" not in names or "[content_types].xml" not in names:
            raise IngestError(
                "xlsx_invalid", "The archive is not an Excel workbook (xl/workbook.xml missing)."
            )
        if any(n.endswith("vbaproject.bin") for n in names):
            raise IngestError(
                "xlsx_macro", "The workbook contains a VBA macro project; macro workbooks are refused."
            )
        content_types = zf.read(names["[content_types].xml"])
        if b"macroenabled" in content_types.lower() or b"vbaproject" in content_types.lower():
            raise IngestError("xlsx_macro", "The workbook declares macro-enabled content; it is refused.")
        if any(n.startswith("xl/externallinks/") for n in names):
            raise IngestError(
                "xlsx_external_link", "The workbook links to external workbooks; such links are refused."
            )
        for prefix in ("xl/activex/", "xl/embeddings/"):
            if any(n.startswith(prefix) for n in names):
                raise IngestError(
                    "xlsx_active_content",
                    "The workbook contains ActiveX controls or embedded OLE objects; refused.",
                )
        for i in infos:
            if not i.filename.lower().endswith((".xml", ".rels", ".vml")):
                continue
            with zf.open(i) as part:
                tail = b""
                while chunk := part.read(_CHUNK):
                    if _DTD.search(tail + chunk):
                        raise IngestError(
                            "xlsx_xml_attack",
                            f"Workbook part {i.filename!r} contains a DTD/entity declaration "
                            "(not used by Excel; "
                            "a known XML attack vector).",
                        )
                    tail = chunk[-16:]


@contextmanager
def _workbook(path: Path, data_only: bool) -> Iterator[Any]:
    """Open a workbook read-only from a file handle (the staged file is never named after the upload)."""
    import openpyxl  # imported lazily: only XLSX uploads need it

    with path.open("rb") as fh:
        try:
            with warnings.catch_warnings():  # cosmetic openpyxl warnings (styles, extensions) are not issues
                warnings.simplefilter("ignore")
                wb = openpyxl.load_workbook(
                    fh, read_only=True, data_only=data_only, keep_links=False, keep_vba=False
                )
        except Exception as exc:  # openpyxl raises many types for corrupt input
            raise IngestError(
                "xlsx_invalid", f"The workbook could not be read: {type(exc).__name__}."
            ) from exc
        try:
            yield wb
        finally:
            wb.close()


def _choose_sheet(wb: Any, sheet: str | None) -> str:
    visible = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
    if sheet is not None:
        if sheet not in [ws.title for ws in wb.worksheets]:
            raise IngestError("xlsx_sheet_not_found", f"Sheet {sheet!r} does not exist.", {"sheets": visible})
        return sheet
    if len(visible) == 1:
        return visible[0]
    if not visible:
        raise IngestError("xlsx_no_sheet", "The workbook has no visible worksheet.")
    raise IngestError(
        "xlsx_sheet_required",
        "The workbook has several sheets; upload again naming the sheet to import.",
        {"sheets": visible},
    )


def open_xlsx(path: Path, cfg: IngestConfig, sheet: str | None = None) -> TableSource:
    _inspect_xlsx_zip(path, cfg)
    with _workbook(path, data_only=False) as wb:
        chosen = _choose_sheet(wb, sheet)
        formulas = sum(
            1
            for row in wb[chosen].iter_rows(max_col=cfg.max_columns + 1)
            for c in row
            if getattr(c, "data_type", None) == "f"
        )
    details = {
        "sheet": chosen,
        "header_row": 1,
        "formula_cells": formulas,
        "values": "cached (formulas never evaluated)",
    }
    return _with_header(TableSource(path, "xlsx", details, []), cfg)


def _xlsx_rows(src: TableSource, cfg: IngestConfig) -> Iterator[tuple[int, list[Cell]]]:
    with _workbook(src.path, data_only=True) as wb:
        rows = wb[src.details["sheet"]].iter_rows(max_col=cfg.max_columns + 1, values_only=True)
        n = 0
        while True:
            # max_col bounds the row width: openpyxl otherwise pads rows to the sheet's declared dimension.
            try:
                row = next(rows)
            except StopIteration:
                return
            except Exception as exc:
                raise IngestError(
                    "xlsx_invalid", f"The worksheet could not be read: {type(exc).__name__}."
                ) from exc
            n += 1
            yield n, list(row)


# ----------------------------------------------------------------------------------------------- common


def _raw_rows(src: TableSource, cfg: IngestConfig) -> Iterator[tuple[int, list[Cell]]]:
    return _csv_rows(src, cfg) if src.format == "csv" else _xlsx_rows(src, cfg)


def _with_header(src: TableSource, cfg: IngestConfig) -> TableSource:
    rows = _raw_rows(src, cfg)
    try:
        for _line, cells in rows:
            if not all(is_blank(c) for c in cells):
                src.header = _resolve_header(cells, cfg.max_columns)
                return src
    finally:
        rows.close()
    raise IngestError("no_header", "The file has no header row (it is empty or blank).")


def reopen(path: Path, fmt: Format, details: dict[str, Any], cfg: IngestConfig) -> TableSource:
    """Reopen stored evidence with the settings detected at upload (re-running the XLSX safety checks)."""
    if fmt == "xlsx":
        _inspect_xlsx_zip(path, cfg)
    return _with_header(TableSource(path, fmt, dict(details), []), cfg)


def iter_records(src: TableSource, cfg: IngestConfig) -> Iterator[tuple[int, list[Cell]]]:
    """Yield (source_row, cells) for every non-blank data record, enforcing row and shape limits.

    After exhaustion `src.stats` holds `records` (data records incl. blank) and `blank_records`.
    """
    width = len(src.header)
    header_seen = False
    records = blank = 0
    rows = _raw_rows(src, cfg)
    try:
        for line, cells in rows:
            if not header_seen:
                if all(is_blank(c) for c in cells):
                    continue
                header_seen = True
                continue
            records += 1
            if records > cfg.max_rows:
                raise IngestError(
                    "too_many_rows",
                    f"The file has more than {cfg.max_rows} data rows (the configured limit).",
                    {"limit": cfg.max_rows},
                )
            where = f"line {line}" if src.format == "csv" else f"sheet row {line}"
            shaped = _shape(cells, width, records, where)
            if shaped is None:
                blank += 1
                continue
            yield records, shaped
    finally:
        rows.close()
    src.stats = {"records": records, "blank_records": blank}

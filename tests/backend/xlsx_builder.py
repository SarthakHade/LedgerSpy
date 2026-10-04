"""Minimal, byte-deterministic XLSX writer for tests.

openpyxl cannot write cached formula results or deliberately hostile parts, so the fixtures for "formulas use
cached values only", macros, external links, XML attacks and zip bombs are assembled from raw parts here.
"""

import io
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_FIXED_TIME = (2024, 1, 1, 0, 0, 0)


class Formula:
    def __init__(self, expr: str, cached: float | None):
        self.expr, self.cached = expr, cached


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _excel_serial(d: date | datetime) -> float:
    base = datetime(1899, 12, 30)
    dt = d if isinstance(d, datetime) else datetime(d.year, d.month, d.day)
    return (dt - base).total_seconds() / 86400


def _cell(ref: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, Formula):
        cached = "" if value.cached is None else f"<v>{value.cached}</v>"
        return f'<c r="{ref}"><f>{escape(value.expr)}</f>{cached}</c>'
    if isinstance(value, date | datetime):
        return f'<c r="{ref}" s="1"><v>{_excel_serial(value)}</v></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def sheet_xml(rows: list[list], prolog: str = "", dimension: str | None = None) -> bytes:
    body = "".join(
        f'<row r="{r + 1}">' + "".join(_cell(f"{_col(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>"
        for r, row in enumerate(rows)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{prolog}'
        f'<worksheet xmlns="{MAIN}">'
        + (f'<dimension ref="{dimension}"/>' if dimension else "")
        + f"<sheetData>{body}</sheetData></worksheet>"
    ).encode()


STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{MAIN}">'
    '<fonts count="1"><font/></fonts>'
    '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
).encode()


def build_xlsx(
    sheets: dict[str, list[list]],
    *,
    extra_parts: dict[str, bytes] | None = None,
    sheet_prolog: str = "",
    workbook_content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    hidden: tuple[str, ...] = (),
    dimension: str | None = None,
) -> bytes:
    names = list(sheets)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(names))
    )
    parts: dict[str, bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{workbook_content_type}"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>"
        ).encode(),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ).encode(),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">'
            "<sheets>"
            + "".join(
                f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"'
                + (' state="hidden"' if n in hidden else "")
                + "/>"
                for i, n in enumerate(names)
            )
            + "</sheets></workbook>"
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                for i in range(len(names))
            )
            + f'<Relationship Id="rIdS" Type="{REL}/styles" Target="styles.xml"/></Relationships>'
        ).encode(),
        "xl/styles.xml": STYLES,
    }
    for i, n in enumerate(names):
        parts[f"xl/worksheets/sheet{i + 1}.xml"] = sheet_xml(
            sheets[n], sheet_prolog if i == 0 else "", dimension if i == 0 else None
        )
    parts.update(extra_parts or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()

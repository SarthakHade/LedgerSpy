"""Tiny, dependency-free PDF writer for tests (P14b): text pages, image pages, optional RC4 encryption.

Generated documents are synthetic test fixtures only. Text uses the standard Helvetica font (WinAnsi, ASCII
here). Image pages embed an 8-bit grayscale raster (FlateDecode). Encryption implements the PDF Standard
Security Handler revision 2 (40-bit RC4) with a USER password, so a reader cannot open the file without it.
"""

import hashlib
import zlib

PAGE = (595, 842)  # A4 in points
_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for b in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_ops(lines: list[tuple[float, float, str]], size: float = 9.0) -> bytes:
    """lines: (x, y_from_top, text) in points."""
    ops = []
    for x, y, t in lines:
        ops.append(f"BT /F1 {size} Tf {x:.2f} {PAGE[1] - y:.2f} Td ({_esc(t)}) Tj ET")
    return "\n".join(ops).encode("latin-1")


def build(pages: list[dict], user_password: str | None = None, garbage_trailer: bool = False) -> bytes:
    """pages: {"text": [(x, y, text), ...], "size": 9}, {"image": (width, height, gray_bytes)} or both."""
    objs: list[bytes] = []  # objs[i] is object number i + 1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_num = add(b"")  # placeholder
    streams: dict[int, bytes] = {}
    kids = []
    for p in pages:
        if "image" in p:
            w, h, data = p["image"]
            img = add(b"")
            streams[img] = zlib.compress(data)
            objs[img - 1] = (
                f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
                f"/BitsPerComponent 8 /Filter /FlateDecode /Length {{LEN}} >>"
            ).encode()
            content = f"q {PAGE[0]} 0 0 {PAGE[1]} 0 0 cm /Im1 Do Q".encode()
            res = f"/Resources << /XObject << /Im1 {img} 0 R >> >>"
            if "text" in p:  # an image AND a text layer on one page (a scan with an existing OCR layer)
                content += b"\n" + text_ops(p["text"], p.get("size", 9.0))
                res = f"/Resources << /XObject << /Im1 {img} 0 R >> /Font << /F1 {font} 0 R >> >>"
        else:
            content = text_ops(p["text"], p.get("size", 9.0))
            res = f"/Resources << /Font << /F1 {font} 0 R >> >>"
        c = add(b"<< /Length {LEN} >>")
        streams[c] = content
        page = add(
            f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 {PAGE[0]} {PAGE[1]}] {res} "
            f"/Contents {c} 0 R >>".encode()
        )
        kids.append(page)
    objs[pages_num - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    )
    catalog = add(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())

    file_id = hashlib.md5(repr(pages).encode(), usedforsecurity=False).digest()
    key = None
    encrypt_num = None
    if user_password is not None:
        p_flags = -4
        owner_key = hashlib.md5((b"owner" + _PAD)[:32], usedforsecurity=False).digest()[:5]
        o_entry = _rc4(owner_key, (user_password.encode() + _PAD)[:32])
        key = hashlib.md5(
            (user_password.encode() + _PAD)[:32]
            + o_entry
            + p_flags.to_bytes(4, "little", signed=True)
            + file_id,
            usedforsecurity=False,
        ).digest()[:5]
        u_entry = _rc4(key, _PAD)
        encrypt_num = add(
            f"<< /Filter /Standard /V 1 /R 2 /O <{o_entry.hex()}> /U <{u_entry.hex()}> "
            f"/P {p_flags} >>".encode()
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        if i in streams:
            data = streams[i]
            if key is not None:
                okey = hashlib.md5(
                    key + i.to_bytes(3, "little") + b"\x00\x00", usedforsecurity=False
                ).digest()[:10]
                data = _rc4(okey, data)
            body = body.replace(b"{LEN}", str(len(data)).encode())
            out += f"{i} 0 obj\n".encode() + body + b"\nstream\n" + data + b"\nendstream\nendobj\n"
        else:
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objs) + 1} /Root {catalog} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]"
    if encrypt_num:
        trailer += f" /Encrypt {encrypt_num} 0 R"
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    if garbage_trailer:
        return bytes(out[: len(out) // 3])
    return bytes(out)


def render_gray(pdf_bytes: bytes, page: int = 0, dpi: int = 300) -> tuple[int, int, bytes]:
    """Rasterise a page of a (text) PDF to grayscale bytes: turns a digital statement into a 'scan'."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        img = doc[page].render(scale=dpi / 72).to_pil().convert("L")
    finally:
        doc.close()
    return img.width, img.height, img.tobytes()


# ------------------------------------------------------------------------------------------ statements

LAYOUTS = {
    # header literal, role, x position (left edge) or ("right", x) for right-aligned numbers
    "withdrawal_deposit": [
        ("Date", "date", 36),
        ("Narration", "description", 100),
        ("Ref No", "reference", 270),
        ("Withdrawal", "debit", ("right", 405)),
        ("Deposit", "credit", ("right", 480)),
        ("Balance", "balance", ("right", 560)),
    ],
    "debit_credit": [
        ("Txn Date", "date", 36),
        ("Value Date", "value_date", 96),
        ("Description", "description", 156),
        ("Debit", "debit", ("right", 405)),
        ("Credit", "credit", ("right", 480)),
        ("Balance", "balance", ("right", 560)),
    ],
    "p5": [
        ("Txn Date", "date", 30),
        ("Narration", "description", 82),
        ("Ref No", "reference", 322),
        ("Withdrawal", "debit", ("right", 445)),
        ("Deposit", "credit", ("right", 505)),
        ("Balance", "balance", ("right", 568)),
    ],
    "amount_drcr": [
        ("Date", "date", 36),
        ("Particulars", "description", 100),
        ("Amount", "amount", ("right", 400)),
        ("Dr/Cr", "drcr", 420),
        ("Balance", "balance", ("right", 560)),
    ],
}


def _width(text: str, size: float) -> float:
    return sum(0.556 if ch.isdigit() or ch in ",.-" else 0.6 for ch in text) * size


def statement(
    rows: list[dict],
    layout: str = "withdrawal_deposit",
    info: list[str] | None = None,
    closing: str | None = None,
    rows_per_page: int = 30,
    footer: bool = True,
    header_every_page: bool = True,
    size: float = 8.0,
) -> list[list[tuple[float, float, str]]]:
    """Text lines per page for a synthetic statement. rows: {role: text, 'more': [continuation narration]}."""
    cols = LAYOUTS[layout]
    pages_out: list[list[tuple[float, float, str]]] = []
    chunks = [rows[i : i + rows_per_page] for i in range(0, len(rows), rows_per_page)] or [[]]
    for pi, chunk in enumerate(chunks):
        lines: list[tuple[float, float, str]] = []
        y = 40.0
        if pi == 0:
            for t in info or []:
                lines.append((36, y, t))
                y += 14
            y += 10
        if pi == 0 or header_every_page:
            for lit, _role, x in cols:
                xx = x[1] - _width(lit, size) if isinstance(x, tuple) else x
                lines.append((xx, y, lit))
            y += 16
        for r in chunk:
            for _lit, role, x in cols:
                v = r.get(role)
                if not v:
                    continue
                xx = x[1] - _width(v, size) if isinstance(x, tuple) else x
                lines.append((xx, y, v))
            y += 12
            for more in r.get("more", []):
                desc_x = next(x for _l, role, x in cols if role == "description")
                lines.append((desc_x, y, more))
                y += 12
        if pi == len(chunks) - 1 and closing:
            y += 8
            lines.append((36, y, closing))
        if footer:
            lines.append((260, 810, f"Page {pi + 1} of {len(chunks)}"))
        pages_out.append(lines)
    return pages_out


def statement_pdf(
    rows, layout="withdrawal_deposit", info=None, closing=None, scanned_pages=(), dpi=200, **kw
) -> bytes:
    """Digital statement; pages in `scanned_pages` (0-based) are replaced by raster images (no text)."""
    pages_lines = statement(rows, layout, info, closing, **kw)
    digital = build([{"text": p, "size": kw.get("size", 8.0)} for p in pages_lines])
    if not scanned_pages:
        return digital
    out = []
    for i, p in enumerate(pages_lines):
        out.append(
            {"image": render_gray(digital, i, dpi)}
            if i in scanned_pages
            else {"text": p, "size": kw.get("size", 8.0)}
        )
    return build(out)

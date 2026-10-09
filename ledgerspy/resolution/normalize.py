"""Deterministic normalisation of party fields for entity resolution (P12).

Every function takes a raw value and returns a `Normalized(raw, value, rules)`: the raw value is kept
verbatim, `value` is the comparison form (None when the raw value is missing or has no usable content) and
`rules` lists exactly which documented rules changed it. Nothing here modifies canonical data: normalised
values exist only inside a resolution run. No value is invented: a field that is absent stays absent.

Rules (docs/entity-resolution.md §2):
- text (all fields): Unicode NFKC, zero-width characters removed, case-folded, whitespace collapsed.
- name: "&" → "and"; dots and apostrophes removed inside words ("Pvt." → "pvt", "L.L.P." → "llp", "O'Neil" →
  "oneil"); hyphens between letters removed ("Quor-ven" → "quorven"); other punctuation → space. A trailing
  run of legal-form tokens is recognised and canonicalised ("Private Limited", "Pvt. Ltd.", "P Ltd" → "pvt
  ltd"; "L.L.P." → "llp"; "& Co" → "and co"): `core` is the name without it and `legal_form` is kept
  separately, never discarded. Business words ("Trading", "Traders", "Supplies") are meaningful and never
  removed.
- person names: as names, plus `tokens_sorted` (word order is not identity: "Rao Gaurav" ~ "Gaurav Rao").
- phone: digits only; a leading "00" international prefix and the configured country code are removed when the
  remainder still has ≥ 8 digits; a single leading trunk "0" is removed. Fewer than 6 digits → unusable.
- email: trimmed and case-folded; must have exactly one "@" with a dotted domain, else unusable. The local
  part is never rewritten (no dot or "+tag" folding: that would invent equivalences).
- address: text rules, punctuation → space, common abbreviations expanded ("Rd" → "road"); a 6-digit postal
  (PIN) code is extracted separately when present.
- bank account: letters and digits only, upper-cased (spaces and dashes are presentation); a bank code
  (IFSC-like routing code) is normalised the same way and kept as a separate field. Fewer than 6 characters
  → unusable.
- tax identifier: letters and digits only, upper-cased. A 15-character GSTIN-shaped value also yields its
  embedded 10-character PAN, so a GSTIN and the PAN it contains compare equal; nothing else is derived.
"""

import re
import unicodedata
from dataclasses import dataclass, field

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_SPACE = re.compile(r"\s+")
_DOT_IN_WORD = re.compile(r"(?<=\w)[.'’](?=\w|\s|$)")
_HYPHEN_IN_WORD = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
_PUNCT = re.compile(r"[^\w\s]")
_PIN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_GSTIN = re.compile(r"^\d{2}([A-Z]{5}\d{4}[A-Z])[A-Z0-9]{3}$")

# Legal forms: token sequence (after punctuation removal) → canonical form. Longest match wins.
LEGAL_FORMS: dict[tuple[str, ...], str] = {
    ("private", "limited"): "pvt ltd",
    ("pvt", "limited"): "pvt ltd",
    ("private", "ltd"): "pvt ltd",
    ("pvt", "ltd"): "pvt ltd",
    ("p", "ltd"): "pvt ltd",
    ("pte", "ltd"): "pvt ltd",
    ("public", "limited", "company"): "plc",
    ("plc",): "plc",
    ("limited",): "ltd",
    ("ltd",): "ltd",
    ("llp",): "llp",
    ("l", "l", "p"): "llp",
    ("llc",): "llc",
    ("inc",): "inc",
    ("incorporated",): "inc",
    ("corp",): "corp",
    ("corporation",): "corp",
    ("gmbh",): "gmbh",
    ("and", "co"): "and co",
    ("and", "company"): "and co",
    ("co",): "co",
    ("company",): "co",
}
_MAX_FORM = max(len(k) for k in LEGAL_FORMS)

ADDRESS_ABBREVIATIONS = {
    "rd": "road",
    "st": "street",
    "ave": "avenue",
    "blvd": "boulevard",
    "ln": "lane",
    "nr": "near",
    "opp": "opposite",
    "bldg": "building",
    "apt": "apartment",
    "flr": "floor",
    "fl": "floor",
    "ind": "industrial",
    "indl": "industrial",
    "est": "estate",
    "sec": "sector",
    "no": "number",
}


@dataclass(frozen=True)
class Normalized:
    raw: str | None
    value: str | None
    rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedName(Normalized):
    core: str | None = None  # the name without its legal form
    legal_form: str | None = None  # canonical legal form, or None
    tokens: tuple[str, ...] = ()  # core tokens in order
    tokens_sorted: tuple[str, ...] = field(default=())


def _text(raw: str | None, rules: list[str]) -> str | None:
    if raw is None:
        return None
    t = unicodedata.normalize("NFKC", str(raw))
    if t != raw:
        rules.append("unicode_nfkc")
    t2 = _ZERO_WIDTH.sub("", t)
    if t2 != t:
        rules.append("zero_width_removed")
    t3 = _SPACE.sub(" ", t2).strip()
    if t3 != t2:
        rules.append("whitespace_collapsed")
    t4 = t3.casefold()
    if t4 != t3:
        rules.append("case_folded")
    return t4 or None


def name(raw: str | None) -> NormalizedName:
    rules: list[str] = []
    t = _text(raw, rules)
    if t is None:
        return NormalizedName(raw, None, tuple(rules))
    s = t.replace("&", " and ")
    if s != t:
        rules.append("ampersand_to_and")
    s2 = _DOT_IN_WORD.sub("", s)
    s2 = _HYPHEN_IN_WORD.sub("", s2)
    if s2 != s:
        rules.append("in_word_punctuation_removed")
    s3 = _PUNCT.sub(" ", s2)
    if s3 != s2:
        rules.append("punctuation_to_space")
    tokens = [tok for tok in s3.replace("_", " ").split() if tok]
    if not tokens:
        return NormalizedName(raw, None, tuple(rules))
    legal = None
    end = len(tokens)
    # strip the longest trailing legal form, repeatedly ("xyz and co pvt ltd" → core "xyz")
    forms = []
    while end > 1:
        for n in range(min(_MAX_FORM, end - 1), 0, -1):
            key = tuple(tokens[end - n : end])
            if key in LEGAL_FORMS:
                forms.insert(0, LEGAL_FORMS[key])
                end -= n
                break
        else:
            break
    if forms:
        legal = " ".join(forms)
        rules.append("legal_form_canonicalised")
    core_tokens = tuple(tokens[:end])
    value = " ".join(core_tokens + ((legal,) if legal else ()))
    return NormalizedName(
        raw,
        value,
        tuple(rules),
        core=" ".join(core_tokens),
        legal_form=legal,
        tokens=core_tokens,
        tokens_sorted=tuple(sorted(core_tokens)),
    )


def phone(raw: str | None, country_code: str) -> Normalized:
    rules: list[str] = []
    if raw is None or not str(raw).strip():
        return Normalized(raw, None)
    digits = re.sub(r"\D", "", str(raw))
    if digits != str(raw):
        rules.append("digits_only")
    if digits.startswith("00") and len(digits) - 2 >= 8:
        digits = digits[2:]
        rules.append("international_prefix_removed")
    if country_code and digits.startswith(country_code) and len(digits) - len(country_code) >= 8:
        digits = digits[len(country_code) :]
        rules.append("country_code_removed")
    if digits.startswith("0") and len(digits) > 6:
        digits = digits[1:]
        rules.append("trunk_zero_removed")
    if len(digits) < 6:
        return Normalized(raw, None, (*rules, "too_short_unusable"))
    return Normalized(raw, digits, tuple(rules))


def email(raw: str | None) -> Normalized:
    rules: list[str] = []
    t = _text(raw, rules)
    if t is None or t.count("@") != 1 or " " in t:
        return Normalized(raw, None, (*rules, "unusable") if t else tuple(rules))
    local, domain = t.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return Normalized(raw, None, (*rules, "unusable"))
    return Normalized(raw, t, tuple(rules))


def email_domain(value: str | None) -> str | None:
    return value.split("@", 1)[1] if value else None


def address(raw: str | None) -> tuple[Normalized, str | None, tuple[str, ...]]:
    """(normalised address, postal code or None, address tokens without the postal code)."""
    rules: list[str] = []
    t = _text(raw, rules)
    if t is None:
        return Normalized(raw, None, tuple(rules)), None, ()
    s = _PUNCT.sub(" ", t)
    if s != t:
        rules.append("punctuation_to_space")
    toks = s.split()
    expanded = [ADDRESS_ABBREVIATIONS.get(x, x) for x in toks]
    if expanded != toks:
        rules.append("abbreviations_expanded")
    pin = None
    m = _PIN.search(" ".join(expanded))
    if m:
        pin = m.group(1)
        rules.append("postal_code_extracted")
    words = tuple(x for x in expanded if x != pin)
    return Normalized(raw, " ".join(expanded) or None, tuple(rules)), pin, words


def bank_account(raw: str | None) -> Normalized:
    if raw is None or not str(raw).strip():
        return Normalized(raw, None)
    v = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    rules = ("alphanumeric_upper",) if v != str(raw) else ()
    if len(v) < 6:
        return Normalized(raw, None, (*rules, "too_short_unusable"))
    return Normalized(raw, v, rules)


def bank_code(raw: str | None) -> Normalized:
    if raw is None or not str(raw).strip():
        return Normalized(raw, None)
    v = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    return Normalized(raw, v or None, ("alphanumeric_upper",) if v != str(raw) else ())


def tax_id(raw: str | None) -> tuple[Normalized, str | None]:
    """(normalised identifier, embedded PAN for a GSTIN-shaped value, else None)."""
    if raw is None or not str(raw).strip():
        return Normalized(raw, None), None
    v = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    rules = ("alphanumeric_upper",) if v != str(raw) else ()
    if len(v) < 5:
        return Normalized(raw, None, (*rules, "too_short_unusable")), None
    m = _GSTIN.match(v)
    return Normalized(raw, v, rules), (m.group(1) if m else None)


def code(raw: str | None) -> Normalized:
    """An account code used as a party name (PaySim): trimmed and upper-cased, compared exactly."""
    if raw is None or not str(raw).strip():
        return Normalized(raw, None)
    v = str(raw).strip().upper()
    return Normalized(raw, v, ("trimmed_upper",) if v != raw else ())

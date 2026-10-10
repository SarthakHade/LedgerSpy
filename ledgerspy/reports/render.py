"""Human-readable renderings of report content (P16): self-contained HTML and editable Markdown.

Every value is escaped: HTML through `html.escape` (no markup from data ever reaches the page; the document
carries a CSP that allows no script, no network and no images), Markdown by escaping HTML-significant and
Markdown control characters, so a hostile note or file name stays text in any viewer. Rendering is a pure
function of the content and the artefact envelope.
"""

import base64
import hashlib
import html
import json
import re
from typing import Any

from ledgerspy.reports import model

STYLE = """
body{font-family:Georgia,'Times New Roman',serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#111}
h1{font-size:1.5rem}h2{font-size:1.2rem;border-bottom:1px solid #999;margin-top:28px}h3{font-size:1rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin:8px 0}
th,td{border:1px solid #bbb;padding:4px 6px;vertical-align:top;text-align:left}th{background:#eee}
.banner{border:2px solid #b45309;background:#fef3c7;padding:8px 12px;margin:12px 0}
.stale{border:2px solid #b91c1c;background:#fee2e2;padding:8px 12px;margin:12px 0}
.meta{font-family:monospace;font-size:.75rem;color:#444;word-break:break-all}
.origin-automated{color:#1e3a8a}.origin-analyst{color:#065f46}.origin-computed{color:#444}
.mono{font-family:monospace;font-size:.75rem;word-break:break-all}
"""

# P20: only this stylesheet may apply (by hash); no script, network, images, forms or base URL.
CSP = (
    "default-src 'none'; style-src 'sha256-"
    + base64.b64encode(hashlib.sha256(STYLE.encode("utf-8")).digest()).decode("ascii")
    + "'; img-src 'none'; base-uri 'none'; form-action 'none'"
)


def h(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return html.escape(json.dumps(value, ensure_ascii=False, sort_keys=True), quote=True)
    return html.escape(str(value), quote=True)


def money(m: dict[str, Any] | None) -> str:
    return "unavailable" if m is None else m["text"]


class _Html:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, raw: str) -> None:
        self.parts.append(raw)

    def h1(self, t: str) -> None:
        self.add(f"<h1>{h(t)}</h1>")

    def h2(self, t: str) -> None:
        self.add(f"<h2>{h(t)}</h2>")

    def h3(self, t: str) -> None:
        self.add(f"<h3>{h(t)}</h3>")

    def p(self, t: Any, cls: str | None = None) -> None:
        self.add(f"<p{f' class={chr(34)}{cls}{chr(34)}' if cls else ''}>{h(t)}</p>")

    def div(self, t: Any, cls: str) -> None:
        self.add(f'<div class="{cls}">{h(t)}</div>')

    def ul(self, items: list[Any]) -> None:
        if not items:
            self.p("None.")
            return
        self.add("<ul>" + "".join(f"<li>{h(i)}</li>" for i in items) + "</ul>")

    def table(self, headers: list[str], rows: list[list[Any]], empty: str = "None.") -> None:
        if not rows:
            self.p(empty)
            return
        head = "".join(f"<th>{h(x)}</th>" for x in headers)
        body = "".join("<tr>" + "".join(f"<td>{h(c)}</td>" for c in r) + "</tr>" for r in rows)
        self.add(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")

    def kv(self, d: dict[str, Any]) -> None:
        self.table(["Field", "Value"], [[k, v] for k, v in d.items()])


def _refs(refs: list[dict[str, Any]]) -> str:
    out = []
    for r in refs:
        locs = "; ".join(_loc(x) for x in r["locations"]) or "no location"
        ev = ", ".join(f"{e['sha256']} ({e['verification']})" for e in r["evidence"]) or "no evidence file"
        out.append(f"{r['link_type']} {r['ref_id']} [{r['verification']}] at {locs}; evidence {ev}")
    return " | ".join(out) or "none"


def _loc(x: dict[str, Any]) -> str:
    bits = []
    if x.get("side"):
        bits.append(x["side"])
    if x.get("filename"):
        bits.append(str(x["filename"]))
    if x.get("source_row") is not None:
        bits.append(f"row {x['source_row']}")
    if x.get("pdf_page") is not None:
        bits.append(f"page {x['pdf_page']}")
    if x.get("pdf_item_id"):
        bits.append(str(x["pdf_item_id"]))
    if x.get("review_state"):
        bits.append(f"review {x['review_state']}")
    return " ".join(bits) or (x.get("evidence_sha256") or "")


def _envelope(doc: _Html, content: dict[str, Any], env: dict[str, Any]) -> None:
    doc.h1(content["title"])
    doc.add(
        '<p class="meta">'
        + " · ".join(
            h(x)
            for x in (
                f"{model.REPORT_TYPES[content['report_type']]}",
                f"report {env.get('report_id') or 'PREVIEW (not stored)'}",
                f"version {env.get('version') or '—'}",
                f"generated {env.get('generated_at') or '—'} by {env.get('generated_by') or '—'}",
                f"content SHA-256 {content['content_sha256']}",
                f"schema {content['schema_version']} · format {content['format_version']} · "
                f"config {content['config_sha256']}",
            )
        )
        + "</p>"
    )
    if content["report_type"] == "audit_memo":
        doc.div(model.DRAFT_NOTICE, "banner")
    doc.div(
        "Automated findings are indicators that require review, not proof of fraud. Review-risk scores are "
        "priorities, not probabilities. Analyst assessments and conclusions are reproduced as recorded.",
        "banner",
    )


def _claims_table(doc: _Html, claims: list[dict[str, Any]], status: bool = False) -> None:
    rows = []
    for c in claims:
        row = [c["claim_id"], c["origin"], c["text"]]
        if status:
            row.append((c.get("analyst_status") or "—").replace("_", " "))
        row += [c["traceability"], _refs(c["references"]), "; ".join(c.get("limitations") or []) or "—"]
        rows.append(row)
    headers = ["Claim", "Origin", "Statement"] + (["Analyst status"] if status else [])
    doc.table(headers + ["Traceability", "References and evidence", "Limitations"], rows)


def _case(doc: _Html, c: dict[str, Any], level: int = 2) -> None:
    hh = doc.h2 if level == 2 else doc.h3
    inv = c["investigation"]
    hh("Engagement")
    doc.kv(c["engagement"])
    hh("Investigation")
    doc.kv(
        {
            k: inv[k]
            for k in (
                "investigation_id",
                "title",
                "description",
                "priority",
                "status",
                "created_by",
                "created_at",
            )
        }
    )
    hh("Conclusion")
    if c["conclusion"]:
        doc.p(f"Analyst-recorded outcome: {c['conclusion']['outcome'].replace('_', ' ')}.", "origin-analyst")
        doc.p(c["conclusion"]["text"], "origin-analyst")
        if c["conclusion"]["closure_rationale"]:
            doc.p(f"Closure rationale: {c['conclusion']['closure_rationale']}", "origin-analyst")
    else:
        doc.p("No conclusion recorded. The report draws none.")
    hh("Scope: datasets and reconciliation runs")
    doc.table(
        ["Dataset", "File", "Kind", "Format", "Rows", "Synthetic", "Evidence SHA-256", "Evidence"],
        [
            [
                d["dataset_id"],
                d["original_filename"],
                d["kind"],
                d["format"],
                d["row_count"],
                d["synthetic"],
                d["evidence_sha256"],
                d["evidence_verification"],
            ]
            for d in c["scope"]["datasets"]
        ],
    )
    doc.table(
        ["Run", "State"],
        [
            [r["run_id"], "STALE — superseded" if r["stale"] else "current"]
            for r in c["scope"]["reconciliation_runs"]
        ],
        "No reconciliation run linked.",
    )
    hh("Automated findings (indicators)")
    _claims_table(doc, c["automated_findings"], status=True)
    hh("Linked records")
    _claims_table(doc, c["linked_records"])
    hh("Analyst assessments")
    doc.table(
        [
            "Decision",
            "Subject",
            "Action",
            "Rationale (analyst)",
            "Supporting",
            "Contradicting",
            "Open",
            "By / at",
        ],
        [
            [
                a["decision_id"],
                a["subject"],
                a["action_text"],
                a["text"],
                _refs(a["references"][1 if a["subject"] != "case" else 0 :]) if a["references"] else "none",
                _refs(a["contradicting"]) if a["contradicting"] else "none recorded",
                a["unresolved"],
                f"{a['created_by']} {a['created_at']}",
            ]
            for a in c["analyst_assessments"]
        ],
        "No analyst assessment recorded.",
    )
    hh("Analyst notes")
    doc.table(
        ["Note", "Subject", "Text (verbatim)", "By / at"],
        [
            [n["note_id"], n["subject"], n["text"], f"{n['created_by']} {n['created_at']}"]
            for n in c["analyst_notes"]
        ],
        "No notes.",
    )
    hh("Open questions")
    doc.ul([f"{u['subject']}: {u['action_text']} — {u['rationale']}" for u in c["unresolved"]])
    hh("Values of linked transactions")
    v = c["linked_values"]
    doc.p(v["note"])
    doc.table(
        ["Dataset", "Currency", "Transactions", "Gross value"],
        [[t["dataset_id"], t["currency"], t["transactions"], money(t["gross_value"])] for t in v["totals"]],
        "No transaction is linked directly.",
    )
    doc.table(
        ["Dataset", "Transaction", "Posted", "Amount", "Overrides (logged P4)", "Location"],
        [
            [
                t["dataset_id"],
                t["txn_id"],
                t["posted_at"],
                money(t["amount"]),
                t["overrides"] or "none",
                _loc(t["location"]) if t["location"] else "record unavailable",
            ]
            for t in v["transactions"]
        ],
        "—",
    )
    hh("Data quality of the references")
    doc.kv(c["data_quality"] or {"all references": "current"})
    hh("Removed links (history)")
    doc.table(
        ["Link", "Type", "Reference", "Linked because", "Removed"],
        [
            [r["link_id"], r["link_type"], r["ref_id"], r["linked_reason"], r["removal"]]
            for r in c["removed_links"]
        ],
        "None.",
    )
    hh("Activity")
    a = c["activity"]
    if a["truncated"]:
        doc.p(f"Showing the first {a['listed']} of {a['total']} events.")
    doc.table(
        ["#", "Event", "Actor", "At", "Status", "Audit seq", "Audit hash"],
        [
            [
                e.get("seq"),
                e.get("event"),
                e.get("actor"),
                e.get("created_at"),
                f"{e.get('from_status') or ''} → {e.get('to_status') or ''}" if e.get("to_status") else "",
                e.get("audit_seq"),
                e.get("audit_hash"),
            ]
            for e in a["items"]
        ],
    )
    hh("Limitations")
    doc.ul(c["limitations"])


def _recon(doc: _Html, c: dict[str, Any]) -> None:
    doc.h2("Engagement")
    doc.kv(c["engagement"])
    doc.h2("Run")
    doc.kv(c["run"])
    doc.h2("Sources")
    doc.table(
        ["Side", "Dataset", "File", "Format", "Rows", "Evidence SHA-256", "Evidence"],
        [
            [
                side,
                d["dataset_id"],
                d["original_filename"],
                d["format"],
                d["row_count"],
                d["evidence_sha256"],
                d["evidence_verification"],
            ]
            for side, d in c["sources"].items()
        ],
    )
    if c["run_state"] != "current":
        doc.div(f"Run state: {c['run_state'].upper()}. {c['run_state_reason']}", "stale")
        doc.h2("Summary recorded at the time (superseded — not a current reconciliation)")
        doc.kv(c["recorded_summary_superseded"])
        doc.h2("Recommendations")
        doc.ul(c["recommendations"])
        return
    sc = c["status_counts"]
    doc.h2("Results by status")
    doc.table(
        ["Status", "Results", "Ledger rows", "Bank rows"],
        [[st, n, sc["rows"]["ledger"][st], sc["rows"]["bank"][st]] for st, n in sc["results"].items()],
    )
    doc.p(sc["matched_row_share_note"])
    doc.h2("Totals by account, currency and status")
    t = c["totals"]
    doc.p(t["note"])
    doc.p(t["check"])
    doc.table(
        ["Side", "Account", "Currency", "Status", "Rows", "Value"],
        [
            [b["side"], b["account"], b["currency"], b["status"], b["rows"], money(b["value"])]
            for b in t["breakdown"]
        ],
    )
    doc.table(
        ["Measure", "Ledger", "Bank"],
        [
            [
                m,
                "; ".join(money(x) for x in t[m]["ledger"]) or "0",
                "; ".join(money(x) for x in t[m]["bank"]) or "0",
            ]
            for m in ("matched", "partial", "ambiguous", "unmatched")
        ],
    )
    ex = c["exceptions"]
    doc.h2("Exceptions (not matched)")
    if ex["truncated"]:
        doc.p(f"Listing {ex['listed']} of {ex['total']} exceptions; the totals above cover all of them.")
    doc.table(
        [
            "Result",
            "Status",
            "Account",
            "Ledger",
            "Bank",
            "Difference",
            "Days",
            "Explanation",
            "Analyst decisions",
        ],
        [
            [
                r["result_id"],
                r["status"],
                r["account"],
                "; ".join(
                    f"{x['txn_id']} row {x['source_row']} {x['date']} {money(x['amount'])}"
                    for x in r["ledger_rows"]
                ),
                "; ".join(
                    f"{x['txn_id']} row {x['source_row']} {x['date']} {money(x['amount'])}"
                    + (
                        f" (PDF page {x['extraction'].get('page')}, {x['extraction'].get('review')})"
                        if x.get("extraction")
                        else ""
                    )
                    for x in r["bank_rows"]
                ),
                money(r["amount_difference"]) if r["amount_difference"] else "—",
                r["date_difference_days"],
                r["explanation"],
                "; ".join(f"{d['action_text']}: {d['rationale']}" for d in r["analyst_decisions"]) or "none",
            ]
            for r in ex["items"]
        ],
        "No exceptions.",
    )
    d = c["duplicate_candidates"]
    doc.h2("Duplicate candidates")
    doc.p(d["note"])
    doc.table(
        ["Group", "Side", "Type", "Transactions", "Amount"],
        [
            [g["group_id"], g["side"], g["type"], ", ".join(g["txn_ids"]), money(g["amount"])]
            for g in d["items"]
        ],
    )
    doc.h2("Balance check")
    doc.kv(c["balance_check"])
    doc.h2("Extracted (PDF / OCR) statement lines")
    doc.kv(c["extraction"])
    doc.h2("Limitations")
    doc.ul(c["limitations"])
    doc.h2("Recommendations (not findings)")
    doc.ul(c["recommendations"])


def _memo(doc: _Html, c: dict[str, Any]) -> None:
    for s in c["sections"]:
        doc.h2(s["title"])
        for para in s.get("paragraphs", []):
            doc.p(para)
        if s.get("items"):
            doc.ul(s["items"])
        if s.get("findings"):
            doc.table(
                ["Claim", "Origin", "Statement", "Evidence SHA-256", "Location"],
                [
                    [
                        f["claim_id"],
                        f["origin"],
                        f["text"],
                        ", ".join(f["evidence"]) or "none",
                        "; ".join(_loc(x) for x in f["locations"]) or "—",
                    ]
                    for f in s["findings"]
                ],
            )
    doc.h2("Facts used by this memo")
    doc.kv({k: v for k, v in c["facts"].items()})
    doc.p(f"Numbers check: {c['numbers_check']['rule']} ({c['numbers_check']['numbers_stated']} numbers).")
    for case in c["case_reports"]:
        doc.h2(f"Appendix — {case['title']}")
        _case(doc, case, level=3)


def _manifest(doc: _Html, c: dict[str, Any]) -> None:
    doc.h2("Engagement")
    doc.kv(c["engagement"])
    doc.h2("Investigations")
    doc.table(
        ["Investigation", "Title", "Status"],
        [[i["investigation_id"], i["title"], i["status"]] for i in c["investigations"]],
    )
    doc.h2("Counts")
    doc.kv(c["counts"])
    doc.p(c["note"])
    doc.h2("Evidence files")
    doc.table(
        ["SHA-256", "Original file names", "Bytes", "Stored", "Verification", "Referenced by"],
        [
            [
                f["sha256"],
                ", ".join(f["original_filenames"]),
                f["size_bytes"],
                f["stored_at"],
                f["verification"],
                ", ".join(f["referenced_by"]),
            ]
            for f in c["evidence_files"]
        ],
    )
    doc.h2("References")
    doc.table(
        [
            "Investigation",
            "Link",
            "Type",
            "Reference",
            "Active",
            "Verification",
            "Locations",
            "Evidence",
            "Linked",
        ],
        [
            [
                e["investigation_id"],
                e["link_id"],
                e["link_type"],
                e["ref_id"],
                e["active"],
                e["verification"],
                "; ".join(_loc(x) for x in e["locations"]) or "—",
                ", ".join(e["evidence_sha256"]),
                f"{e['linked_by']} {e['linked_at']}: {e['reason']}",
            ]
            for e in c["references"]
        ],
    )


def to_html(content: dict[str, Any], env: dict[str, Any]) -> str:
    doc = _Html()
    _envelope(doc, content, env)
    {
        "case_investigation": _case,
        "bank_reconciliation": _recon,
        "audit_memo": _memo,
        "evidence_manifest": _manifest,
    }[content["report_type"]](doc, content)
    doc.h2("Provenance")
    doc.kv(content["provenance"])
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{h(content['title'])}</title><style>{STYLE}</style></head><body>\n"
        + "\n".join(doc.parts)
        + "\n</body></html>\n"
    )


# ------------------------------------------------------------------------------------------- markdown

_MD_SPECIAL = re.compile(r"([\\`*_\[\]#+!|])")


def md(value: Any) -> str:
    """Escape one value for Markdown: HTML-significant characters become entities, Markdown control
    characters are backslash-escaped, line breaks become spaces (so a value cannot start a new block)."""
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = " ".join(text.splitlines())
    return _MD_SPECIAL.sub(r"\\\1", text)


def to_markdown(content: dict[str, Any], env: dict[str, Any]) -> str:
    """An editable text rendering: the memo's sections, or a report's headline sections. The JSON artefact
    remains the complete record."""
    out = [f"# {md(content['title'])}", ""]
    out.append(
        f"*{md(model.REPORT_TYPES[content['report_type']])} · report {md(env.get('report_id') or 'PREVIEW')} "
        f"· version {md(env.get('version'))} · generated {md(env.get('generated_at'))} · content SHA-256 "
        f"{md(content['content_sha256'])}*"
    )
    out.append("")
    if content["report_type"] == "audit_memo":
        out += [f"> **{md(model.DRAFT_NOTICE)}**", ""]
        for s in content["sections"]:
            out += [f"## {md(s['title'])}", ""]
            out += [md(p) + "\n" for p in s.get("paragraphs", [])]
            out += [f"- {md(i)}" for i in s.get("items", [])]
            out += [
                f"- {md(f['text'])} (claim {md(f['claim_id'])}; "
                f"evidence {md(', '.join(f['evidence']) or 'none')})"
                for f in s.get("findings", [])
            ]
            out.append("")
    elif content["report_type"] == "case_investigation":
        inv = content["investigation"]
        out += [
            f"## {md('Investigation')}",
            "",
            f"{md(inv['investigation_id'])} — {md(inv['title'])} — status {md(inv['status'])}",
            "",
        ]
        out += [f"## {md('Automated findings (indicators)')}", ""]
        out += [
            f"- {md(f['text'])} — analyst status {md(f['analyst_status'])}; {md(f['traceability'])}"
            for f in content["automated_findings"]
        ] or ["None."]
        out += ["", f"## {md('Analyst assessments')}", ""]
        out += [
            f"- {md(a['action_text'])} on {md(a['subject'])}: {md(a['text'])}"
            for a in content["analyst_assessments"]
        ] or ["None."]
        out += ["", f"## {md('Conclusion')}", ""]
        c = content["conclusion"]
        out.append(f"{md(c['outcome'])}: {md(c['text'])}" if c else "No conclusion recorded.")
    elif content["report_type"] == "bank_reconciliation":
        out += [f"Run state: **{md(content['run_state'])}** {md(content['run_state_reason'] or '')}", ""]
        if content["run_state"] == "current":
            out += [f"## {md('Results by status')}", ""]
            out += [f"- {md(st)}: {md(n)}" for st, n in content["status_counts"]["results"].items()]
            out += ["", f"## {md('Totals by account, currency and status')}", ""]
            out += [
                f"- {md(b['side'])} {md(b['account'])} {md(b['currency'])} {md(b['status'])}: "
                f"{md(b['rows'])} rows, {md(money(b['value']))}"
                for b in content["totals"]["breakdown"]
            ]
    else:
        out += [f"## {md('Evidence files')}", ""]
        out += [
            f"- {md(f['sha256'])} {md(', '.join(f['original_filenames']))}: {md(f['verification'])}"
            for f in content["evidence_files"]
        ]
    out += ["", "_The JSON artefact is the complete record; this text is a convenience rendering._", ""]
    return "\n".join(out)

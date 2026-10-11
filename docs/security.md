# LedgerSpy Security Notes and Limitations

A summary for users and administrators.
- **Full threat model, controls and their tests:** [`p20-security-acceptance.md`](p20-security-acceptance.md).
- **Every open requirement:** [`pre-p23-gap-audit.md`](pre-p23-gap-audit.md) and
  [`acceptance-inventory.json`](acceptance-inventory.json).

LedgerSpy is **not** claimed to be "secure" in general, "tamper-proof" or "court-ready". Below, *controls* are
what the software enforces; *limits* are what it cannot guarantee.

## 1. Deployment it is designed for

One analyst on one Windows workstation:
- LedgerSpy runs as that user and listens on `127.0.0.1` only;
- the analyst's browser opens the printed link;
- the workspace lives in the user's profile;
- no network service is used, and nothing is sent anywhere.

Any other deployment is unsupported, for example:
- exposing the port;
- a shared or network workspace;
- several users on one workspace;
- running the API under a hand-written server command.

## 2. Threat model in brief

| Adversary | In scope? | What LedgerSpy does |
|---|---|---|
| **A1** Hostile content: a crafted CSV / XLSX / PDF, OCR text, file names, notes | Yes | Bounded parsers, refusal of macros / external links / encrypted or malformed files, PDF work in a time-limited subprocess, rendering as text only, strict CSP |
| **A2** Another website in the same browser (CSRF, DNS rebinding, cross-origin reads) | Yes | Host and Origin checks, Fetch-Metadata refusal of cross-site requests, the token only in the `Authorization` header |
| **A3** Another local process or user without the token | Yes | Every API call except `/api/health` needs the per-launch token |
| **A4** A local administrator, or malware running as the user | **Prevention: no.** Detection: partly (§4) | Nothing LedgerSpy does can stop someone who controls the files and the code. Use full-disk encryption and separate OS accounts |

## 3. Controls (enforced)

**Network**
- Binds only to `127.0.0.1`; there is no option to listen elsewhere.
- No outbound connection at run time. The offline check of the bundle observed 0 non-loopback connection
  attempts (see the limits in §5).
- The UI loads nothing from the internet.

**Session token**
- Random per start (at least 32 characters), compared in constant time.
- Accepted for API calls only in the `Authorization: Bearer` header. The `?token=` in the entry link is exchanged
  once for an `HttpOnly; SameSite=Strict` cookie that unlocks only the page.
- Never written to the database, the audit log or an HTTP access log.

**Browser**
- Content-Security-Policy `script-src 'self'; style-src 'self'`; the server refuses to start if the page contains
  inline script or style.
- Every imported value is displayed as text.
- Report HTML carries its own no-script CSP.

**Requests**
- Bodies up to 8 MiB (uploads stream within their own 100 MiB limit), bounded pagination, strict request models.
- No endpoint takes a file path.

**Evidence**
- Every imported file is stored once, read-only, under its SHA-256, and re-hashed before use.
- File names are display metadata only; control and bidirectional-override characters are refused.

**Records**
- Append-only database triggers on every record table: transformations, investigations, decisions, notes,
  registers, the audit log.
- Every write is in the hash-chained audit log with the local OS identity.

**Integrity checks**
- `ledgerspy audit verify` checks the chain and every trigger.
- `ledgerspy evidence verify` re-hashes the evidence.
- Reports are re-hashed on every read.

**Models**
- Supervised model artefacts are JSON only (never pickle), hash-pinned in a registry, never loaded from uploads.
- Labels and ground truth never reach the detectors, the risk score or reports.

**Packaging**
- A pinned and hash-locked wheelhouse is installed with `--no-index --require-hashes`.
- Each bundle has a manifest of SHA-256 values and records its build commit.

## 4. The audit trail: what it can and cannot show

| Change | Detected? |
|---|---|
| Update or delete of a record through SQLite | **Prevented** by triggers |
| An edited, inserted, reordered or deleted audit entry | **Yes** (hash chain) |
| A dropped, altered or extra trigger | **Yes** (trigger check) |
| An altered evidence file or report artefact | **Yes** (SHA-256) |
| Truncation of the newest entries, or a consistent rewrite of the whole chain | **Only with an external anchor** recorded beforehand ([`operations.md`](operations.md) §6) |
| An edit made while a trigger was dropped and then restored exactly | **No** |

The log is **tamper-evident, not tamper-proof**. The anchor check is available only through the API, and an
anchor kept on the same machine can be altered with the database.

## 5. Known limits

**Access and identity**
- **Access.** Anyone who holds the session token, or who controls the user's account, has full access. There are
  no roles, no multi-user identity, no assignment and no second-person approval.
- **The token can linger.** It may remain in the terminal scroll-back and in the browser history entry of the
  `Open:` link until the server stops. Browser extensions can read the page.
- **No HTTPS** on loopback, so the cookie cannot be `Secure`. Traffic never leaves the machine.

**Resources and platforms**
- **No memory cap** on the PDF / OCR worker or Tesseract. Limits are 200 pages, 40 Mpx per page, 120 s per OCR
  page and 15 min per extraction. A very large file can use a lot of memory.
- **Platform scope.** Built and verified on **Windows 11 x64 with CPython 3.12** only. Other operating systems
  and architectures are **unverified**. The bundle is not a standalone executable: Python 3.12 and, for OCR,
  Tesseract 5 are separate prerequisites.

**Offline evidence**
- The automated check isolated the installed application's processes with a network guard. It did **not**
  disable the machine's network adapter.
- A fresh install on the **target laptop with networking physically disabled** has not been performed by the
  developers. It is the user's acceptance step.

**Data coverage**
- **Bank layouts.** PDF parsing recognises header-anchored column tables only. Real bank-statement layouts have
  not been validated, and non-English OCR and handwriting are untested. Unrecognised lines are reported, never
  guessed.

**Process gaps**
- **Dependency audit.** `pip-audit` has not been run: it needs network access and belongs on the connected build
  machine.
- **Fuzzing** is a seeded smoke fuzz, not coverage-guided.

## 6. Important deferred items

Features that do not exist, so that nobody relies on them. Each is classified in the gap audit (§4 there).

| Area | Not built / not done |
|---|---|
| Reports | **No native PDF or DOCX export** (JSON, HTML and Markdown only; HTML can be printed to PDF by the browser, unhashed). **No memo approval or sign-off**: memos are always drafts. No report or run comparison |
| Risk | Reconciliation exceptions, graph signals and supervised models do **not** feed the review-risk score. P9 has no recorded scenario-recall target |
| Reconciliation | No aging, no currency conversion. Grouping only for contiguous runs. References of different kinds are not compared. Row-level results of a stale run are not kept |
| Workflow | Single local user. Accepted entity links and resolved ambiguities are recorded assessments that do not feed back into merging or matching. Master data is imported by command line only. Case calendar and approval limits only by configuration file |
| PDF / OCR | Other layouts, encrypted PDFs (no password entry), Dr/Cr-signed balance checks, a worker memory cap |
| Packaging and testing | No standalone executable. No non-Windows bundle. No coverage measurement. No 1M-row performance test. Detached verification runs do not survive a reboot |
| Verification on the user's machine | Fully disconnected target-laptop test, `pip-audit`, real bank statements |

**Optional or data-gated phases.** The investigation assistant (P17), Monte Carlo cash flow (P18) and
benchmarking (P19) are not built. The UI shows Monte Carlo and benchmarking as "Not available". The chat
assistant answers only from the analysis API.

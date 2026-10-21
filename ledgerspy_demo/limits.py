"""Demo limits: conservative, configurable, and never above the offline application's own limits.

Every limit has a default here and can be lowered (or raised up to its ceiling) with an environment
variable `LEDGERSPY_DEMO_<NAME>` (upper case). Where the offline configuration has an equivalent limit, the
demo value is checked against it: the demo may be stricter, never looser. Invalid values are refused, not
clamped.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

MIB = 1024 * 1024


class DemoConfigError(ValueError):
    """A demo setting is missing, malformed or outside its allowed range."""


@dataclass(frozen=True)
class DemoLimits:
    # per upload
    max_upload_bytes: int = 2 * MIB  # offline: ingest.max_file_bytes (100 MiB)
    max_rows: int = 20_000  # offline: ingest.max_rows (1,000,000)
    max_columns: int = 64  # offline: ingest.max_columns (512)
    # XLSX: checked on the zip directory before any parsing (offline: 500 MiB, 10,000 entries)
    xlsx_max_uncompressed_bytes: int = 16 * MIB
    xlsx_max_zip_entries: int = 100
    max_pdf_pages: int = 5  # offline: pdf.max_pages (200)
    pdf_worker_timeout_seconds: int = 120  # offline: pdf.worker_timeout_seconds (900)
    # one rendered PDF page (OCR or preview), the bound on its temporary PNG: an A4 / Letter / Legal page at
    # 300 dpi is at most about 10.7 Mpx; larger pages are refused, never downscaled (offline: 40,000,000)
    max_pdf_render_pixels: int = 12_000_000
    # file output of one operation, enforced before anything is written (offline: unlimited)
    max_report_bytes: int = 4 * MIB  # all artifacts of one generated report (measured: 0.81 MiB on the seed)
    max_inference_bytes: int = 4 * MIB  # one stored supervised-inference result
    # PDF worker jobs (upload of a bank statement, re-extraction, page image) running at once, service-wide
    max_concurrent_pdf_jobs: int = 1
    # per workspace
    max_uploads_per_workspace: int = 10
    max_workspace_bytes: int = 25 * MIB  # database + evidence + reports, i.e. the snapshot size
    max_processing_seconds: int = 120  # one analysis / reconciliation / report request
    requests_per_minute: int = 60
    uploads_per_hour: int = 10
    retention_days: int = 7
    lock_wait_seconds: int = 30  # a request waiting longer for its workspace gets 503 Retry-After
    idempotency_ttl_seconds: int = 600  # how long a completed request can be replayed by its Idempotency-Key
    # per service
    max_active_workspaces: int = 20  # workspaces on disk (not expired)
    # workspace app instances in memory at once: each holds its cached results (~30 MiB measured after the
    # main analyses) and, while writing, up to ~1.2 x its database cap of dirty pages (512 MB host)
    max_resident_workspaces: int = 2
    new_workspaces_per_hour_per_client: int = 5
    new_workspaces_per_hour: int = 20  # service-wide, whatever the client identity (spoof-proof cap)
    shutdown_grace_seconds: int = 25  # graceful stop: wait this long for running work, then stop PDF workers
    # snapshots (only with storage configured): after this many quiet seconds, or at the latest this long
    # after
    # the first unsaved change
    snapshot_quiet_seconds: int = 20
    snapshot_max_delay_seconds: int = 120
    storage_budget_bytes: int = 800 * MIB  # under Supabase Free's 1 GB, with head-room

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise DemoConfigError(f"{f.name} must be a positive integer, got {v!r}")
        if self.retention_days > 7:
            raise DemoConfigError("retention_days must not exceed 7 (the agreed public-demo retention)")
        if self.max_workspace_bytes < self.max_upload_bytes:
            raise DemoConfigError("max_workspace_bytes must be at least max_upload_bytes")
        if self.storage_budget_bytes < 2 * self.max_workspace_bytes:
            raise DemoConfigError("storage_budget_bytes must be at least twice max_workspace_bytes")
        if self.max_resident_workspaces > self.max_active_workspaces:
            raise DemoConfigError("max_resident_workspaces must not exceed max_active_workspaces")
        if self.new_workspaces_per_hour_per_client > self.new_workspaces_per_hour:
            raise DemoConfigError(
                "new_workspaces_per_hour_per_client must not exceed new_workspaces_per_hour"
            )

    def check_against_offline(self, settings: Any) -> None:
        """Refuse any demo limit looser than its offline equivalent (settings: EffectiveConfig.settings)."""
        ceilings = {
            "max_upload_bytes": settings.ingest.max_file_bytes,
            "max_rows": settings.ingest.max_rows,
            "max_columns": settings.ingest.max_columns,
            "xlsx_max_uncompressed_bytes": settings.ingest.xlsx.max_uncompressed_bytes,
            "xlsx_max_zip_entries": settings.ingest.xlsx.max_zip_entries,
            "max_pdf_pages": settings.pdf.max_pages,
            "pdf_worker_timeout_seconds": settings.pdf.worker_timeout_seconds,
            "max_pdf_render_pixels": settings.pdf.render.max_pixels,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise DemoConfigError(
                    f"{name}={getattr(self, name)} exceeds the offline limit {ceiling}; "
                    "the demo may only be stricter"
                )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DemoLimits":
        values: dict[str, int] = {}
        for f in fields(cls):
            raw = env.get(f"LEDGERSPY_DEMO_{f.name.upper()}")
            if raw is None:
                continue
            text = raw.strip()
            if not text.isdigit():
                raise DemoConfigError(
                    f"LEDGERSPY_DEMO_{f.name.upper()} must be a positive integer, got {raw!r}"
                )
            values[f.name] = int(text)
        return cls(**values)

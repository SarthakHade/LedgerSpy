"""Server-side workspace identity: IDs are generated here, carried in an HMAC-signed cookie, and turned
into local paths and object-storage keys only after validation. Nothing a visitor sends is ever used as a
path or key.
"""

import hashlib
import hmac
import re
import secrets
from pathlib import Path

WORKSPACE_ID = re.compile(r"^W-[0-9a-f]{32}$")
_COOKIE = re.compile(r"^(W-[0-9a-f]{32})\.([0-9]{1,12})\.([0-9a-f]{64})$")
_DOMAIN = b"ledgerspy-demo-workspace-cookie-v1"
CLOCK_SKEW_SECONDS = 60


def new_workspace_id() -> str:
    """128 random bits; never derived from client input, time or a counter."""
    return "W-" + secrets.token_hex(16)


def is_workspace_id(value: object) -> bool:
    return isinstance(value, str) and WORKSPACE_ID.fullmatch(value) is not None


def _mac(key: bytes, workspace_id: str, issued_at: int) -> str:
    msg = b"|".join((_DOMAIN, workspace_id.encode("ascii"), str(issued_at).encode("ascii")))
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_cookie(key: bytes, workspace_id: str, issued_at: int) -> str:
    if not is_workspace_id(workspace_id):
        raise ValueError("not a server-generated workspace ID")
    if len(key) < 32:
        raise ValueError("signing key too short")
    if issued_at < 0:
        raise ValueError("issued_at must be a Unix time")
    return f"{workspace_id}.{issued_at}.{_mac(key, workspace_id, issued_at)}"


def read_cookie(key: bytes, value: str | None, now: int, max_age_seconds: int) -> str | None:
    """The workspace ID of a genuine, unexpired cookie; None for anything else (missing, malformed, forged,
    expired, issued in the future). The caller then starts a NEW workspace; it never trusts the value."""
    if not value or len(value) > 200:
        return None
    m = _COOKIE.fullmatch(value)
    if m is None:
        return None
    workspace_id, issued_text, mac = m.groups()
    issued_at = int(issued_text)
    if not hmac.compare_digest(mac, _mac(key, workspace_id, issued_at)):
        return None
    if issued_at > now + CLOCK_SKEW_SECONDS or now - issued_at > max_age_seconds:
        return None
    return workspace_id


def workspace_dir(root: Path | str, workspace_id: str) -> Path:
    """The local folder of a validated workspace ID, guaranteed to be a direct child of `root`."""
    if not is_workspace_id(workspace_id):
        raise ValueError("not a server-generated workspace ID")
    base = Path(root).resolve()
    path = (base / workspace_id).resolve()
    if path.parent != base:
        raise ValueError("workspace path escapes the data folder")
    return path


def snapshot_object_key(workspace_id: str, generation: int) -> str:
    """Private-bucket object key of one immutable snapshot generation. Keys are never reused."""
    if not is_workspace_id(workspace_id):
        raise ValueError("not a server-generated workspace ID")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")
    return f"workspaces/{workspace_id}/snapshot-{generation:012d}.tar.gz"


def manifest_object_key(workspace_id: str) -> str:
    """The pointer to the workspace's latest complete snapshot (written only after that upload)."""
    if not is_workspace_id(workspace_id):
        raise ValueError("not a server-generated workspace ID")
    return f"workspaces/{workspace_id}/manifest.json"

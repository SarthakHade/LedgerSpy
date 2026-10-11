"""Locations of application-level resources: LedgerSpy runs from its application folder (a source checkout or
the P21 offline bundle), never from an installed wheel, so the UI and the default configuration sit next to
the package."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
FRONTEND_DIR = REPO_ROOT / "frontend"

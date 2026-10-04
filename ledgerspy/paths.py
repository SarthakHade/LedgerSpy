"""Locations of repository-level resources (run from a source checkout; offline packaging is P21)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
FRONTEND_DIR = REPO_ROOT / "frontend"

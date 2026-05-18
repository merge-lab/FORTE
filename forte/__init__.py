"""FORTE: real-time tactile force estimation and slip detection."""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

__all__ = ["PACKAGE_ROOT", "REPO_ROOT"]

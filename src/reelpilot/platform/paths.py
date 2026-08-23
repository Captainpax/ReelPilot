"""Resolve source-checkout, packaged, and per-user ReelPilot paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def application_data_directory() -> Path:
    r"""Return ``%LOCALAPPDATA%\ReelPilot`` without creating it."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "ReelPilot"


def bundle_directory() -> Path:
    """Return the executable folder when frozen or repository root from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def input_helper_path() -> Path:
    """Return the first installed or source-built native input helper path."""
    candidates = (
        bundle_directory() / "native" / "reelpilot-input.exe",
        bundle_directory() / "_internal" / "native" / "reelpilot-input.exe",
        bundle_directory()
        / "native"
        / "reelpilot-input"
        / "target"
        / "release"
        / "reelpilot-input.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]

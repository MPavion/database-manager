"""Notion Local Sync application package."""

from __future__ import annotations

import os
from pathlib import Path


def configure_qt_environment() -> str:
    """Point Qt at a real font folder before any QApplication is created."""
    existing = str(os.environ.get("QT_QPA_FONTDIR", "") or "").strip()
    if existing and Path(existing).exists():
        return existing

    candidates = []
    windir = str(os.environ.get("WINDIR", "") or "").strip()
    if windir:
        candidates.append(Path(windir) / "Fonts")

    candidates.extend(
        [
            Path(r"C:\Windows\Fonts"),
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
        ]
    )

    for candidate in candidates:
        try:
            if candidate.exists():
                os.environ["QT_QPA_FONTDIR"] = str(candidate)
                return str(candidate)
        except OSError:
            continue

    return existing


configure_qt_environment()

__all__ = ["configure_qt_environment"]

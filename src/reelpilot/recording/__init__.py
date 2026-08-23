"""Opt-in diagnostic recording and deterministic offline replay."""

from .recorder import SessionRecorder
from .replay import ReplayRunner

__all__ = ["ReplayRunner", "SessionRecorder"]

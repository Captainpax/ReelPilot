"""Structural interfaces that keep automation independent of concrete services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import numpy as np

from .domain import (
    BiteObservation,
    CastObservation,
    CatchObservation,
    EnergyObservation,
    FishingObservation,
    FoodPromptObservation,
    RuntimeSnapshot,
    TreasureLootObservation,
)


class VisionPort(Protocol):
    """Provide analyzed scene observations without exposing capture internals."""

    def observe_scene(
        self, expected_bar_length_pixels: int | None = None
    ) -> FishingObservation:
        """Capture and analyze the current minigame scene."""
        ...

    def begin_cast(self) -> None:
        """Reset temporal cast-meter acquisition."""
        ...

    def observe_cast(self) -> CastObservation:
        """Capture and analyze the current cast meter."""
        ...

    def observe_bite(self) -> BiteObservation:
        """Capture the playable ROI and return localized bite evidence."""
        ...

    def detect_text(self, expected_text: str) -> bool:
        """Return whether bounded OCR sees ``expected_text`` in the current frame."""
        ...

    def read_catch(self) -> CatchObservation:
        """Capture and conservatively read a catch-result card."""
        ...

    def observe_treasure_loot(self) -> TreasureLootObservation:
        """Capture and locate a post-catch treasure item menu."""
        ...

    def observe_energy(self) -> EnergyObservation:
        """Capture and measure the right-HUD energy meter."""
        ...

    def observe_food_prompt(self) -> FoodPromptObservation:
        """Capture and confirm an English eating prompt and Yes button."""
        ...

    def latest_frame(self) -> np.ndarray | None:
        """Return the most recent analyzed frame without copying it."""
        ...

    def latest_catch_card(self) -> np.ndarray | None:
        """Return the last localized catch-card crop, if any."""
        ...

    def close(self) -> None:
        """Release capture resources idempotently."""
        ...


class InputPort(Protocol):
    """Expose fail-safe mouse operations to the automation engine."""

    def set_duty(self, duty_ratio: float) -> None:
        """Set the native 20 ms pulse-loop duty ratio."""
        ...

    def press(self) -> None:
        """Press the left mouse button after focusing Stardew."""
        ...

    def release(self) -> None:
        """Release the left mouse button unconditionally."""
        ...

    def tap(self, duration_seconds: float = 0.04) -> None:
        """Perform one bounded press/release action."""
        ...

    def idle(self) -> None:
        """Stop duty control and release held input."""
        ...

    def prepare_menu_capture(self) -> None:
        """Release input and park the cursor outside item-menu source slots."""
        ...

    def click_at(self, x_pixels: int, y_pixels: int) -> None:
        """Click one window-relative point after idling fishing input."""
        ...

    def right_click_at(self, x_pixels: int, y_pixels: int) -> None:
        """Right-click one window-relative point and guarantee button-up."""
        ...

    def tap_key(self, virtual_key: int) -> None:
        """Tap one Windows virtual key while Stardew is focused."""
        ...

    def close(self) -> None:
        """Shut down the helper and release input idempotently."""
        ...


class DashboardPort(Protocol):
    """Receive immutable snapshots and deduplicated human-readable events."""

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        """Publish the newest render-ready snapshot."""
        ...

    def log(self, message: str, *, level: str = "info") -> None:
        """Append a user-facing event at ``level``."""
        ...

    def close(self) -> None:
        """Stop rendering and print a final summary idempotently."""
        ...


class Clock(Protocol):
    """Provide injectable monotonic seconds for deterministic tests."""

    def __call__(self) -> float:
        """Return monotonically increasing seconds."""
        ...


Sleep = Callable[[float], None]

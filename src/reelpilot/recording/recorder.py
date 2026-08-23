"""Bounded background writer for opt-in JSONL telemetry and lossless crops."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any

import cv2
import numpy as np


class SessionRecorder:
    """Record every core event while dropping optional images under backpressure."""

    FORMAT_VERSION = 7
    DEFAULT_IMAGE_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

    def __init__(
        self,
        root: Path,
        settings: dict[str, object],
        *,
        image_queue_size: int = 32,
        image_limit_bytes: int = DEFAULT_IMAGE_LIMIT_BYTES,
    ) -> None:
        """Create a timestamped session and start its dedicated writer thread."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = root / timestamp
        self.frames_directory = self.path / "frames"
        self.frames_directory.mkdir(parents=True, exist_ok=False)
        self.telemetry_path = self.path / "telemetry.jsonl"
        self.manifest_path = self.path / "manifest.json"
        self._telemetry: Queue[
            tuple[dict[str, Any], tuple[Path, str, np.ndarray] | None]
        ] = Queue()
        self._maximum_pending_images = max(0, image_queue_size)
        self._pending_images = 0
        self._image_lock = Lock()
        self._stop = Event()
        self._closed = False
        self._sequence = 0
        self.dropped_images = 0
        self.recorded_image_bytes = 0
        self.image_limit_bytes = max(0, image_limit_bytes)
        self.image_limit_reached = False
        self._settings = settings
        self._created_at_utc = datetime.now(UTC).isoformat()
        self._thread = Thread(target=self._writer, name="reelpilot-recorder", daemon=True)
        self._write_manifest(complete=False)
        self._thread.start()

    @property
    def writer_complete(self) -> bool:
        """Return whether the background writer has exited."""
        return not self._thread.is_alive()

    def record(
        self,
        event: str,
        state: str,
        timestamp_seconds: float,
        data: dict[str, object] | None = None,
        image: tuple[str, np.ndarray] | None = None,
    ) -> None:
        """Queue an ordered telemetry event and optional immutable image reference."""
        if self._closed:
            return
        self._sequence += 1
        item: dict[str, Any] = {
            "sequence": self._sequence,
            "timestamp_seconds": timestamp_seconds,
            "event": event,
            "state": state,
        }
        if data:
            item.update(data)
        image_payload: tuple[Path, str, np.ndarray] | None = None
        if image is not None:
            kind, pixels = image
            relative = Path("frames") / f"{self._sequence:07d}-{kind}.png"
            with self._image_lock:
                if (
                    not self.image_limit_reached
                    and self._pending_images < self._maximum_pending_images
                ):
                    # ScreenCapture allocates a new array for every frame. Keeping
                    # that immutable frame alive transfers ownership cheaply; PNG
                    # encoding happens on the writer thread.
                    self._pending_images += 1
                    image_payload = (self.path / relative, kind, pixels)
                else:
                    self.dropped_images += 1
        self._telemetry.put_nowait((item, image_payload))

    def close(self, timeout_seconds: float = 2.0) -> None:
        """Flush within a bound and mark the manifest complete when successful."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout_seconds))
        self._write_manifest(complete=not self._thread.is_alive())

    def _writer(self) -> None:
        with self.telemetry_path.open("a", encoding="utf-8") as stream:
            while not self._stop.is_set() or not self._telemetry.empty():
                try:
                    item, image_payload = self._telemetry.get(timeout=0.02)
                except Empty:
                    continue
                if image_payload is not None:
                    path, kind, image = image_payload
                    # At shutdown, retain the final image but discard an image
                    # backlog so core telemetry reaches disk within the bound.
                    drop_for_shutdown = self._stop.is_set() and not self._telemetry.empty()
                    with self._image_lock:
                        if drop_for_shutdown or self.recorded_image_bytes >= self.image_limit_bytes:
                            self.image_limit_reached |= (
                                self.recorded_image_bytes >= self.image_limit_bytes
                            )
                            self.dropped_images += 1
                        elif cv2.imwrite(str(path), image):
                            self.recorded_image_bytes += path.stat().st_size
                            item["image"] = path.relative_to(self.path).as_posix()
                            item["image_kind"] = kind
                            if self.recorded_image_bytes >= self.image_limit_bytes:
                                self.image_limit_reached = True
                        self._pending_images -= 1
                stream.write(json.dumps(item, separators=(",", ":")) + "\n")
                self._telemetry.task_done()
                if self._telemetry.empty() or self._stop.is_set():
                    stream.flush()

    def _write_manifest(self, *, complete: bool) -> None:
        manifest = {
            "format_version": self.FORMAT_VERSION,
            "created_at_utc": self._created_at_utc,
            "application": "ReelPilot",
            "settings": self._settings,
            "display_baseline": {"game_zoom_percent": 75, "ui_scale_percent": 75},
            "complete": complete,
            "dropped_images": self.dropped_images,
            "recorded_image_bytes": self.recorded_image_bytes,
            "image_limit_bytes": self.image_limit_bytes,
            "image_limit_reached": self.image_limit_reached,
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

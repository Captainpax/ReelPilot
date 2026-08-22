from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any

import cv2
import numpy as np


class SessionRecorder:
    FORMAT_VERSION = 3

    def __init__(
        self,
        root: Path,
        settings: dict[str, object],
        *,
        image_queue_size: int = 128,
    ) -> None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = root / timestamp
        self.frames_directory = self.path / "frames"
        self.frames_directory.mkdir(parents=True, exist_ok=False)
        self.telemetry_path = self.path / "telemetry.jsonl"
        self.manifest_path = self.path / "manifest.json"
        self._telemetry: Queue[dict[str, Any]] = Queue()
        self._images: Queue[tuple[Path, np.ndarray]] = Queue(maxsize=image_queue_size)
        self._stop = Event()
        self._closed = False
        self._sequence = 0
        self.dropped_images = 0
        self._settings = settings
        self._thread = Thread(target=self._writer, name="reelpilot-recorder", daemon=True)
        self._write_manifest(complete=False)
        self._thread.start()

    def record(
        self,
        event: str,
        state: str,
        timestamp_seconds: float,
        data: dict[str, object] | None = None,
        image: tuple[str, np.ndarray] | None = None,
    ) -> None:
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
        if image is not None:
            kind, pixels = image
            relative = Path("frames") / f"{self._sequence:07d}-{kind}.png"
            try:
                self._images.put_nowait((self.path / relative, pixels.copy()))
                item["image"] = relative.as_posix()
                item["image_kind"] = kind
            except Full:
                self.dropped_images += 1
        self._telemetry.put_nowait(item)

    def close(self, timeout_seconds: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=max(0.0, timeout_seconds))
        self._write_manifest(complete=not self._thread.is_alive())

    def _writer(self) -> None:
        with self.telemetry_path.open("a", encoding="utf-8") as stream:
            while (
                not self._stop.is_set()
                or not self._telemetry.empty()
                or not self._images.empty()
            ):
                wrote = False
                try:
                    item = self._telemetry.get(timeout=0.02)
                except Empty:
                    pass
                else:
                    stream.write(json.dumps(item, separators=(",", ":")) + "\n")
                    self._telemetry.task_done()
                    wrote = True
                try:
                    path, image = self._images.get_nowait()
                except Empty:
                    if wrote:
                        stream.flush()
                else:
                    cv2.imwrite(str(path), image)
                    self._images.task_done()

    def _write_manifest(self, *, complete: bool) -> None:
        manifest = {
            "format_version": self.FORMAT_VERSION,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "application": "ReelPilot",
            "settings": self._settings,
            "display_baseline": {"game_zoom_percent": 75, "ui_scale_percent": 75},
            "complete": complete,
            "dropped_images": self.dropped_images,
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

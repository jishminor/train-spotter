"""Utilities for broadcasting frames to the web dashboard."""

from __future__ import annotations

import threading
import time
from typing import Generator, Optional


class FrameBroadcaster:
    """Stores the latest frame and notifies clients when updates arrive."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: Optional[bytes] = None
        self._closed = False

    def update_frame(self, frame: bytes) -> None:
        with self._condition:
            if self._closed:
                return
            self._frame = frame
            self._condition.notify_all()

    def mjpeg_stream(self, fps: int = 10) -> Generator[bytes, None, None]:
        boundary = b"--frame\r\n"
        frame_interval = 1.0 / max(fps, 1)
        while True:
            with self._condition:
                if self._closed:
                    break
                if self._frame is None:
                    self._condition.wait(timeout=frame_interval)
                    continue
                frame = self._frame
            payload = (
                boundary
                + b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
            yield payload
            time.sleep(frame_interval)
        yield b""

    def latest_frame(self) -> Optional[bytes]:
        with self._condition:
            return self._frame

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


__all__ = ["FrameBroadcaster"]

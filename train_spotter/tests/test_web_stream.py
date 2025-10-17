"""Tests for web streaming helper."""

from __future__ import annotations

import itertools

from train_spotter.web import FrameBroadcaster


def test_frame_broadcaster_streams_latest_frame() -> None:
    broadcaster = FrameBroadcaster()
    frame = b"\x00" * 10
    broadcaster.update_frame(frame)

    gen = broadcaster.mjpeg_stream(fps=1)
    chunk = next(gen)

    assert b"--frame" in chunk
    assert b"Content-Type: image/jpeg" in chunk
    assert b"Content-Length: 10" in chunk
    broadcaster.close()

    # Exhaust generator after close
    remaining = b"".join(itertools.islice(gen, 1))
    assert remaining == b""

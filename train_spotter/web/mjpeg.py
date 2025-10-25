"""Lightweight MJPEG-over-WebSocket broadcaster for fallback streaming."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

LOGGER = logging.getLogger(__name__)


class MJPEGStreamServer:
    """Broadcast JPEG frames to subscribers over a dedicated WebSocket endpoint."""

    def __init__(
        self,
        host: str,
        port: int,
        max_clients: int,
        framerate: int,
    ) -> None:
        self._host = host
        self._port = port
        self._max_clients = max_clients
        self._framerate = max(1, framerate)
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[websockets.server.Serve] = None
        self._clients: Set[WebSocketServerProtocol] = set()
        self._frame_queue: Optional[asyncio.Queue[bytes]] = None  # Created in _run() after event loop is set
        self._broadcaster_task: Optional[asyncio.Task[None]] = None
        self._running = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            LOGGER.warning("MJPEG stream server already running")
            return
        LOGGER.info("Starting MJPEG WebSocket server on %s:%s", self._host, self._port)
        self._thread = threading.Thread(target=self._run, name="mjpeg-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._loop.is_running():
            return
        LOGGER.info("Stopping MJPEG WebSocket server")

        async def _shutdown() -> None:
            self._running.clear()
            if self._frame_queue is not None:
                await self._frame_queue.put(b"")
            if self._broadcaster_task:
                try:
                    await self._broadcaster_task
                except Exception:  # pragma: no cover - defensive
                    LOGGER.exception("MJPEG broadcaster task failed during shutdown")
            for client in list(self._clients):
                try:
                    await client.close(code=1001, reason="server-shutdown")
                except Exception:
                    LOGGER.debug("Failed to close MJPEG client cleanly", exc_info=True)
            if self._server is not None:
                self._server.ws_server.close()
                await self._server.ws_server.wait_closed()
                self._server = None
            self._clients.clear()
            self._loop.stop()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        try:
            fut.result(timeout=3)
        except Exception:  # pragma: no cover - best effort shutdown
            LOGGER.exception("Failed to stop MJPEG WebSocket server cleanly")
        if self._thread:
            self._thread.join(timeout=3)

    def publish_frame(self, frame: bytes) -> None:
        if not frame:
            LOGGER.debug("publish_frame called with empty frame, ignoring")
            return
        if not self._running.is_set():
            LOGGER.debug("publish_frame called but server not running, ignoring")
            return
        if self._frame_queue is None:
            LOGGER.debug("publish_frame called but queue not yet initialized, ignoring")
            return

        def _enqueue() -> None:
            if not self._running.is_set():
                LOGGER.debug("_enqueue: server stopped, not queuing frame")
                return
            if self._frame_queue is None:
                LOGGER.debug("_enqueue: queue disappeared, not queuing frame")
                return
            was_full = self._frame_queue.full()
            if was_full:
                try:
                    dropped = self._frame_queue.get_nowait()
                    LOGGER.debug("Queue full, dropped frame (%d bytes) to make room", len(dropped))
                except asyncio.QueueEmpty:
                    pass
            self._frame_queue.put_nowait(frame)

        try:
            self._loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            LOGGER.warning("MJPEG event loop not ready; dropping frame (%d bytes)", len(frame))

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._running.set()
        # Create queue AFTER setting event loop so it's bound to the correct loop
        self._frame_queue = asyncio.Queue(maxsize=2)
        self._server = websockets.serve(
            self._handle_client,
            self._host,
            self._port,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        )
        self._loop.run_until_complete(self._server)
        self._broadcaster_task = asyncio.ensure_future(self._broadcast_frames())
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop=self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _broadcast_frames(self) -> None:
        min_interval = 1.0 / float(self._framerate)
        last_sent = 0.0
        frame_count = 0
        LOGGER.info("MJPEG broadcaster started with framerate=%d (min_interval=%.3fs)", self._framerate, min_interval)
        while self._running.is_set():
            frame = await self._frame_queue.get()
            if not self._running.is_set():
                LOGGER.debug("MJPEG broadcaster stopping (running flag cleared)")
                break
            if not frame:
                LOGGER.debug("MJPEG broadcaster got empty frame, continuing")
                continue
            if not self._clients:
                LOGGER.debug("No MJPEG clients connected, skipping frame (%d bytes)", len(frame))
                continue
            now = asyncio.get_event_loop().time()
            elapsed = now - last_sent
            if last_sent > 0 and elapsed < min_interval:
                continue
            last_sent = now
            frame_count += 1
            stale_clients: list[WebSocketServerProtocol] = []
            for client in list(self._clients):
                try:
                    await client.send(frame)
                except websockets.exceptions.ConnectionClosed:
                    LOGGER.debug("Client %s connection closed", client.remote_address)
                    stale_clients.append(client)
                except Exception:
                    LOGGER.warning("Failed to push MJPEG frame to client %s", client.remote_address, exc_info=True)
                    stale_clients.append(client)
            for client in stale_clients:
                self._clients.discard(client)

    async def _handle_client(self, websocket: WebSocketServerProtocol, path: str) -> None:
        if path not in {"/mjpeg", "/mjpegs", "/stream"}:
            await websocket.close(code=1008, reason="invalid-endpoint")
            return
        if len(self._clients) >= self._max_clients:
            await websocket.close(code=1013, reason="too-many-clients")
            return
        LOGGER.info("MJPEG client connected from %s", websocket.remote_address)
        self._clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            LOGGER.info("MJPEG client disconnected from %s", websocket.remote_address)
            self._clients.discard(websocket)


__all__ = ["MJPEGStreamServer"]


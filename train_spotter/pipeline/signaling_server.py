"""Async WebSocket signaling server bridging browsers to WebRTC manager."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Awaitable, Callable, Optional

import websockets
from websockets.server import WebSocketServerProtocol

from train_spotter.web.webrtc import WebRTCManager, WebRTCSession, parse_browser_payload

LOGGER = logging.getLogger(__name__)


class WebRTCSignalingServer:
    """Expose WebRTC signaling over a standalone WebSocket endpoint."""

    def __init__(
        self,
        host: str,
        port: int,
        manager: WebRTCManager,
    ) -> None:
        self._host = host
        self._port = port
        self._manager = manager
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[websockets.server.Serve] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            LOGGER.warning("Signaling server already running")
            return
        LOGGER.info("Starting WebRTC signaling server on %s:%s", self._host, self._port)
        self._thread = threading.Thread(target=self._run, name="webrtc-signaling", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._loop.is_running():
            return
        LOGGER.info("Stopping WebRTC signaling server")

        async def _shutdown() -> None:
            if self._server is not None:
                self._server.ws_server.close()
                await self._server.ws_server.wait_closed()
                self._server = None
            tasks = [task for task in asyncio.all_tasks(loop=self._loop) if not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._loop.stop()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        try:
            fut.result(timeout=3)
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Failed to stop signaling server cleanly")
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._server = websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
            ping_interval=None,
            reuse_address=True,
        )
        self._loop.run_until_complete(self._server)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop=self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _handle_connection(self, websocket: WebSocketServerProtocol, _path: str) -> None:
        LOGGER.info("Signaling client connected from %s", websocket.remote_address)
        session = self._manager.create_session()
        stop_event = asyncio.Event()
        LOGGER.debug("Created WebRTC session %s for signaling client", session.id)

        loop = asyncio.get_running_loop()

        async def _pump_outgoing() -> None:
            try:
                while not stop_event.is_set():
                    message = await loop.run_in_executor(None, session.next_outgoing, 0.5)
                    if message is None:
                        if session.is_closed():
                            break
                        continue
                    await websocket.send(json.dumps(message))
            except asyncio.CancelledError:  # pragma: no cover - cancellation
                pass
            except Exception:  # pragma: no cover - runtime safeguard
                LOGGER.exception("Failed to send signaling message for session %s", session.id)
            finally:
                stop_event.set()

        outgoing_task = asyncio.ensure_future(_pump_outgoing())

        try:
            async for raw in websocket:
                payload = parse_browser_payload(raw)
                if payload is None:
                    continue
                LOGGER.info(
                    "Received signaling message for session %s: %s",
                    session.id,
                    payload.get("type"),
                )
                session.enqueue_from_browser(payload)
        except websockets.exceptions.ConnectionClosedOK as exc:
            LOGGER.info(
                "Signaling client closed session %s (code=%s, reason=%s)",
                session.id,
                exc.code,
                exc.reason,
            )
        except websockets.exceptions.ConnectionClosedError as exc:
            LOGGER.info(
                "Signaling client dropped session %s (code=%s, reason=%s)",
                session.id,
                exc.code,
                exc.reason,
            )
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Unexpected signaling error for session %s", session.id)
        finally:
            session.close("signaling-closed")
            stop_event.set()
            outgoing_task.cancel()
            self._manager.remove_session(session.id)
            try:
                await outgoing_task
            except Exception:
                pass
            LOGGER.info("Signaling session %s cleaned up", session.id)


__all__ = ["WebRTCSignalingServer"]

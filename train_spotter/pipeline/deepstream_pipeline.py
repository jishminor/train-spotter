"""DeepStream pipeline scaffolding for train spotter application."""

from __future__ import annotations

import configparser
import logging
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, TYPE_CHECKING
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GObject", "2.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstVideo", "1.0")

from gi.repository import GLib, GObject, Gst, GstSdp, GstWebRTC, GstVideo  # type: ignore

from train_spotter.pipeline.analytics import StreamAnalytics, analytics_pad_probe
from train_spotter.service.config import AppConfig
from train_spotter.service.roi import ROIConfig
from train_spotter.storage import EventBus, EventMessage, EventType

if TYPE_CHECKING:
    from train_spotter.web.webrtc import WebRTCManager, WebRTCSession
    from train_spotter.web.mjpeg import MJPEGStreamServer

LOGGER = logging.getLogger(__name__)


@dataclass
class _WebRTCBranch:
    session: "WebRTCSession"
    tee_pad: Gst.Pad
    elements: list[Gst.Element]
    request_pads: list[Tuple[Gst.Element, Gst.Pad]]
    webrtcbin: Gst.Element
    drain_source_id: Optional[int] = None
    poll_source_id: Optional[int] = None  # For polling ICE state (GStreamer 1.16.x workaround)


@dataclass
class _MjpegBranch:
    tee_pad: Gst.Pad
    elements: list[Gst.Element]
    appsink: Gst.Element
    signal_id: Optional[int]


class DeepStreamPipeline:
    """Build and control the DeepStream pipeline according to application config."""

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        roi_config: ROIConfig | None = None,
        webrtc_manager: "WebRTCManager | None" = None,
        mjpeg_server: "MJPEGStreamServer | None" = None,
        enable_inference: bool = True,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._pipeline: Optional[Gst.Pipeline] = None
        self._main_loop: Optional[GObject.MainLoop] = None
        self._bus_watch_id: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._enable_inference = enable_inference
        self._analytics = (
            StreamAnalytics(config, event_bus, roi_config)
            if enable_inference
            else None
        )
        self._tee_src_pads: list[Gst.Pad] = []
        self._source_is_live: bool = True
        self._stop_event = threading.Event()
        self._webrtc_manager = webrtc_manager
        self._webrtc_branches: Dict[str, _WebRTCBranch] = {}
        self._tee: Optional[Gst.Element] = None
        Gst.init(None)
        self._webrtc_supported = self._detect_webrtc_support() if self._webrtc_manager else False
        self._mjpeg_server = mjpeg_server
        self._mjpeg_branch: Optional[_MjpegBranch] = None
        self._mjpeg_logged_first_frame = False
        if self._webrtc_manager is not None:
            self._webrtc_manager.register_session_handler(self._request_webrtc_session)

    def _detect_webrtc_support(self) -> bool:
        webrtc_factory = Gst.ElementFactory.find("webrtcbin")
        if webrtc_factory is None:
            LOGGER.error("WebRTC streaming unavailable: GStreamer lacks the webrtcbin plugin.")
            return False
        nicesrc = Gst.ElementFactory.find("nicesrc")
        nicesink = Gst.ElementFactory.find("nicesink")
        if nicesrc is None or nicesink is None:
            LOGGER.error(
                "WebRTC streaming unavailable: libnice elements not found. Install the "
                "'gstreamer1.0-nice' package to enable WebRTC output."
            )
            return False
        LOGGER.debug("WebRTC support detected (webrtcbin + libnice available).")
        return True

    def build(self) -> None:
        LOGGER.info("Building DeepStream pipeline")
        self._pipeline = Gst.Pipeline.new("train_spotter_pipeline")
        if self._pipeline is None:
            raise RuntimeError("Failed to create DeepStream pipeline")

        source_bin = self._create_source_bin(self._config.camera_source)
        streammux = self._make_element("nvstreammux", "stream-muxer")
        streammux.set_property("batch-size", 1)
        streammux.set_property("width", 640)
        streammux.set_property("height", 640)
        streammux.set_property("enable-padding", True)
        streammux.set_property("live-source", 1 if self._source_is_live else 0)
        streammux.set_property("batched-push-timeout", 4000000)

        primary_infer = None
        tracker = None
        if self._enable_inference:
            primary_infer = self._make_element("nvinfer", "primary-infer")
            primary_infer.set_property(
                "config-file-path", self._config.vehicle_tracking.infer_primary_config_path
            )

            tracker = self._make_element("nvtracker", "tracker")
            tracker_config_path = self._config.vehicle_tracking.tracker_config_path
            if tracker_config_path:
                self._configure_tracker(tracker, tracker_config_path)

        nvvidconv = self._make_element("nvvideoconvert", "video-converter")
        nvosd = self._make_element("nvdsosd", "on-screen-display")

        tee = self._make_element("tee", "output-tee")
        queue_sink = self._make_element("queue", "sink-queue")
        queue_sink.set_property("leaky", 2)
        queue_sink.set_property("max-size-buffers", 4)
        sink = self._make_element("fakesink", "null-sink")
        sink.set_property("sync", False)
        sink.set_property("async", False)

        elements = [streammux]
        if primary_infer is not None:
            elements.append(primary_infer)
        if tracker is not None:
            elements.append(tracker)
        elements.extend(
            [
                nvvidconv,
                nvosd,
                tee,
                queue_sink,
                sink,
            ]
        )

        self._pipeline.add(source_bin)
        for element in elements:
            self._pipeline.add(element)

        self._link_source_to_streammux(source_bin, streammux)
        upstream = streammux
        if self._enable_inference:
            if primary_infer is None or tracker is None:
                raise RuntimeError("Inference components were not initialised properly")
            if not upstream.link(primary_infer):
                raise RuntimeError("Failed to link streammux to nvinfer")
            if not primary_infer.link(tracker):
                raise RuntimeError("Failed to link nvinfer to tracker")
            upstream = tracker
        if not upstream.link(nvvidconv):
            raise RuntimeError("Failed to link previous element to nvvidconv")
        if not nvvidconv.link(nvosd):
            raise RuntimeError("Failed to link nvvidconv to nvosd")
        if not nvosd.link(tee):
            raise RuntimeError("Failed to link nvosd to tee")
        self._link_tee_to_queue(tee, queue_sink)
        if not queue_sink.link(sink):
            raise RuntimeError("Failed to link sink queue to fakesink")
        self._tee = tee

        if self._enable_inference and self._analytics is not None:
            tracker_src_pad = tracker.get_static_pad("src") if tracker is not None else None
            if tracker_src_pad:
                tracker_src_pad.add_probe(
                    Gst.PadProbeType.BUFFER, analytics_pad_probe, self._analytics
                )
            else:
                LOGGER.warning("Failed to attach analytics probe; tracker src pad missing")

        self._log_pipeline_layout()
        self._ensure_mjpeg_branch()

    def start(self) -> None:
        if self._pipeline is None:
            self.build()
        self._stop_event.clear()
        if self._main_loop is not None:
            LOGGER.warning("Pipeline already running")
            return

        self._main_loop = GObject.MainLoop()
        bus = self._pipeline.get_bus()
        self._bus_watch_id = bus.add_watch(GLib.PRIORITY_DEFAULT, self._on_bus_message, None)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Pipeline failed to start")
        if ret == Gst.StateChangeReturn.ASYNC:
            for attempt in range(6):
                change, state, pending = self._pipeline.get_state(Gst.SECOND * 5)
                LOGGER.debug("Pipeline async transition step %d: %s (state=%s pending=%s)", attempt + 1, change, state, pending)
                if change == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING:
                    break
                if change == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Pipeline failed during async state change")
            else:
                LOGGER.warning("Pipeline did not reach PLAYING state; last state=%s pending=%s", state, pending)

    def stop(self) -> None:
        if self._pipeline is None:
            self._stop_event.set()
            return
        LOGGER.info("Stopping DeepStream pipeline")
        self._shutdown_webrtc_sessions("pipeline-stopped")
        self._teardown_mjpeg_branch()

        pipeline = self._pipeline

        def _shutdown_pipeline() -> bool:
            try:
                if pipeline is not None:
                    pipeline.set_state(Gst.State.NULL)
            except Exception:
                LOGGER.exception("Failed to set pipeline state to NULL")
            if self._main_loop:
                try:
                    self._main_loop.quit()
                except Exception:
                    LOGGER.exception("Failed to quit main loop")
            return False

        current_thread = threading.current_thread()
        if self._thread and self._thread.is_alive() and current_thread is not self._thread:
            if self._main_loop:
                GLib.idle_add(_shutdown_pipeline, priority=GLib.PRIORITY_HIGH)
            else:
                _shutdown_pipeline()
            self._thread.join(timeout=5.0)
        else:
            _shutdown_pipeline()

        if self._bus_watch_id is not None:
            GLib.source_remove(self._bus_watch_id)
            self._bus_watch_id = None
        for pad in self._tee_src_pads:
            if pad:
                parent = pad.get_parent()
                if parent and hasattr(parent, "release_request_pad"):
                    parent.release_request_pad(pad)
        self._tee_src_pads.clear()
        self._main_loop = None
        self._pipeline = None
        self._stop_event.set()

    def _run_loop(self) -> None:
        assert self._main_loop is not None
        try:
            self._main_loop.run()
        except Exception:  # pragma: no cover - runtime safeguard
            LOGGER.exception("Main loop terminated unexpectedly")

    def _on_bus_message(self, bus, message, _user_data):
        msg_type = message.type
        if msg_type == Gst.MessageType.EOS:
            LOGGER.info("Pipeline received EOS")
            self.stop()
        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            LOGGER.error("Pipeline error: %s (%s)", err, debug)
            self.stop()
        elif msg_type == Gst.MessageType.STATE_CHANGED:
            if message.src == self._pipeline:
                old_state, new_state, _pending = message.parse_state_changed()
                LOGGER.debug("Pipeline state changed: %s -> %s", old_state, new_state)
        self._event_bus.publish(EventMessage(EventType.HEARTBEAT, None, timestamp=time.time()))
        return True

    def _make_element(self, factory_name: str, name: str):
        element = Gst.ElementFactory.make(factory_name, name)
        if not element:
            raise RuntimeError(f"Failed to create element {factory_name}")
        return element

    def _configure_tracker(self, tracker, config_path: str) -> None:
        parser = configparser.ConfigParser()
        parser.read(config_path)
        if "tracker" not in parser:
            raise RuntimeError(f"Tracker config missing [tracker] section: {config_path}")

        section = parser["tracker"]
        config_dir = Path(config_path).parent

        int_keys = {
            "tracker-width",
            "tracker-height",
            "gpu-id",
            "enable-batch-process",
            "enable-past-frame",
            "display-tracking-id",
            "min-tracks-considered",
            "max-shadow-tracking-age",
        }
        float_keys = {"iou-threshold", "min-confidence"}

        for key, raw_value in section.items():
            prop_name = key
            value: object = raw_value
            if prop_name in {"ll-lib-file", "ll-config-file"}:
                path = Path(raw_value)
                if not path.is_absolute():
                    path = (config_dir / path).resolve()
                value = str(path)
            elif prop_name in int_keys:
                value = int(raw_value)
            elif prop_name in float_keys:
                value = float(raw_value)

            try:
                tracker.set_property(prop_name, value)
            except TypeError:
                LOGGER.warning("Tracker property '%s' not supported; skipping", prop_name)

    def _create_source_bin(self, source_desc: str):
        source_desc = source_desc.strip()
        file_location = self._extract_file_location(source_desc)
        if file_location:
            file_path = self._resolve_file_path(file_location)
            LOGGER.debug("Constructing file source bin for %s", file_path)
            self._source_is_live = False
            return self._create_file_source_bin(file_path)

        # Support both minimal and fully-specified pipelines; ensure a queue terminates the bin
        if "!" in source_desc:
            parts = [segment.strip() for segment in source_desc.split("!") if segment.strip()]
            if not parts:
                raise RuntimeError("Camera source description is empty after parsing")
            if not parts[-1].startswith("queue"):
                parts.append("queue")
            bin_desc = " ! ".join(parts)
        else:
            bin_desc = (
                f"{source_desc} ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! queue"
            )
        LOGGER.debug("Source bin description: %s", bin_desc)
        self._source_is_live = True
        source_bin = Gst.parse_bin_from_description(bin_desc, True)
        source_bin.set_name("source-bin")
        return source_bin

    def _extract_file_location(self, source_desc: str) -> Optional[str]:
        if not source_desc:
            return None
        if source_desc.startswith("file://"):
            return source_desc
        if source_desc.startswith("filesrc"):
            first_segment = source_desc.split("!", 1)[0]
            tokens = shlex.split(first_segment)
            for idx, token in enumerate(tokens[1:], start=1):
                if token.startswith("location="):
                    return token.split("=", 1)[1]
                if token == "location" and idx + 1 < len(tokens):
                    return tokens[idx + 1]
        return None

    def _resolve_file_path(self, location: str) -> str:
        if location.startswith("file://"):
            parsed = urlparse(location)
            path = unquote(parsed.path)
        else:
            path = location
        resolved = Path(path).expanduser()
        try:
            return str(resolved.resolve())
        except FileNotFoundError:
            LOGGER.warning("File source %s does not exist; pipeline may fail", resolved)
            return str(resolved)

    def _create_file_source_bin(self, location: str) -> Gst.Bin:
        self._source_is_live = False
        bin_ = Gst.Bin.new("source-bin")
        file_src = self._make_element("filesrc", "file-source")
        file_src.set_property("location", location)
        demux = self._make_element("qtdemux", "file-demux")
        parser = self._make_element("h264parse", "file-h264parse")
        parser.set_property("config-interval", -1)
        decoder = self._make_element("nvv4l2decoder", "file-decoder")
        converter = self._make_element("nvvideoconvert", "file-converter")
        capsfilter = self._make_element("capsfilter", "file-capsfilter")
        capsfilter.set_property(
            "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12")
        )
        video_queue = self._make_element("queue", "file-video-queue")
        video_queue.set_property("leaky", 2)
        video_queue.set_property("max-size-buffers", 4)

        audio_queue = self._make_element("queue", "file-audio-queue")
        audio_queue.set_property("max-size-buffers", 4)
        audio_queue.set_property("leaky", 2)
        audio_sink = self._make_element("fakesink", "file-audio-fakesink")
        audio_sink.set_property("sync", False)
        audio_sink.set_property("async", False)

        for element in (
            file_src,
            demux,
            parser,
            decoder,
            converter,
            capsfilter,
            video_queue,
            audio_queue,
            audio_sink,
        ):
            bin_.add(element)

        if not file_src.link(demux):
            raise RuntimeError("Failed to link filesrc to demux")
        if not parser.link(decoder):
            raise RuntimeError("Failed to link parser to decoder")
        if not decoder.link(converter):
            raise RuntimeError("Failed to link decoder to converter")
        if not converter.link(capsfilter):
            raise RuntimeError("Failed to link converter to capsfilter")
        if not capsfilter.link(video_queue):
            raise RuntimeError("Failed to link capsfilter to queue")
        if not audio_queue.link(audio_sink):
            raise RuntimeError("Failed to link audio queue to fakesink")

        def _handle_pad(demuxer, pad):
            caps = pad.get_current_caps()
            media_type = caps.get_structure(0).get_name() if caps and caps.get_size() > 0 else ""
            if media_type.startswith("video/"):
                sink_pad = parser.get_static_pad("sink")
            elif media_type.startswith("audio/"):
                sink_pad = audio_queue.get_static_pad("sink")
            else:
                sink_pad = None

            if sink_pad is None or sink_pad.is_linked():
                return

            result = pad.link(sink_pad)
            if result != Gst.PadLinkReturn.OK:
                LOGGER.warning(
                    "Failed to link demux pad %s to %s: %s",
                    pad.get_name(),
                    sink_pad.get_parent_element().get_name()
                    if sink_pad.get_parent_element()
                    else "sink",
                    result,
                )

        demux.connect("pad-added", _handle_pad)

        ghost_pad = video_queue.get_static_pad("src")
        if ghost_pad is None:
            raise RuntimeError("Video queue missing src pad")
        bin_.add_pad(Gst.GhostPad.new("src", ghost_pad))
        return bin_

    def wait_for_stop(self, timeout: Optional[float] = None) -> bool:
        """Block until the pipeline has stopped or timeout expires."""

        return self._stop_event.wait(timeout)

    def has_stopped(self) -> bool:
        """Return True if the pipeline has been stopped."""

        return self._stop_event.is_set()

    def _log_pipeline_layout(self) -> None:
        if not LOGGER.isEnabledFor(logging.DEBUG) or self._pipeline is None:
            return
        try:
            iterator = self._pipeline.iterate_elements()
            element_descriptions: list[str] = []
            while True:
                res, element = iterator.next()
                if res == Gst.IteratorResult.OK and element is not None:
                    factory = element.get_factory()
                    if factory is not None:
                        element_descriptions.append("%s[%s]" % (element.get_name(), factory.get_name()))
                    else:
                        element_descriptions.append(element.get_name())
                elif res == Gst.IteratorResult.DONE:
                    break
                elif res == Gst.IteratorResult.RESYNC:
                    iterator = self._pipeline.iterate_elements()
                elif res == Gst.IteratorResult.ERROR:
                    break
            if element_descriptions:
                LOGGER.info("Pipeline elements: %s", " -> ".join(element_descriptions))
        except Exception:
            LOGGER.exception("Failed to log pipeline elements")

    def _link_source_to_streammux(self, source_bin, streammux):
        sinkpad = streammux.get_request_pad("sink_0")
        if sinkpad is None:
            raise RuntimeError("Unable to request sink_0 pad from streammux")
        iterator = source_bin.iterate_src_pads()
        res, srcpad = iterator.next()
        if res != Gst.IteratorResult.OK or srcpad is None:
            raise RuntimeError("Source bin does not expose a src pad")
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link source bin to streammux")

    def _link_tee_to_queue(self, tee, queue) -> Gst.Pad:
        srcpad = tee.get_request_pad("src_%u")
        if not srcpad:
            raise RuntimeError("Failed to request src pad from tee")
        sinkpad = queue.get_static_pad("sink")
        if not sinkpad:
            raise RuntimeError("Queue missing sink pad")
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link tee to queue")
        self._tee_src_pads.append(srcpad)
        return srcpad

    def _request_webrtc_session(self, session: "WebRTCSession") -> None:
        if not self._webrtc_supported:
            LOGGER.error(
                "WebRTC session %s requested but libnice components are missing. "
                "Install 'gstreamer1.0-nice' and restart to enable WebRTC streaming.",
                session.id,
            )
            session.send_to_browser({"type": "error", "reason": "webrtc-unavailable"})
            session.close("webrtc-unavailable")
            return
        if self._main_loop is None:
            LOGGER.warning("WebRTC session %s requested while pipeline inactive", session.id)
            session.close("pipeline-inactive")
            return

        def _attach_session() -> bool:
            if self._pipeline is None or self._tee is None:
                LOGGER.warning("Pipeline not ready for WebRTC session %s", session.id)
                session.close("pipeline-inactive")
                return False
            if session.id in self._webrtc_branches:
                LOGGER.debug("WebRTC session %s already attached", session.id)
                return False
            try:
                branch = self._create_webrtc_branch(session)
            except Exception:
                LOGGER.exception("Failed to create WebRTC branch for session %s", session.id)
                session.close("pipeline-error")
                return False
            self._webrtc_branches[session.id] = branch
            session.add_close_callback(lambda _: self._schedule_webrtc_teardown(session.id))
            branch.drain_source_id = GLib.timeout_add(50, self._drain_session_messages, session.id)
            LOGGER.info("WebRTC session %s attached", session.id)
            return False

        GLib.idle_add(_attach_session, priority=GLib.PRIORITY_HIGH)

    def _create_webrtc_branch(self, session: "WebRTCSession") -> _WebRTCBranch:
        assert self._pipeline is not None and self._tee is not None

        # --- Elements -------------------------------------------------------------
        queue = self._make_element("queue", f"webrtc-queue-{session.id}")
        queue.set_property("leaky", 2)
        queue.set_property("max-size-buffers", 1)

        converter = self._make_element("nvvideoconvert", f"webrtc-converter-{session.id}")

        # Ensure encoder gets NV12 in NVMM
        caps_in = self._make_element("capsfilter", f"webrtc-caps-{session.id}")
        caps_in.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))

        encoder = self._make_element("nvv4l2h264enc", f"webrtc-encoder-{session.id}")
        encoder.set_property("control-rate", 1)          # 1 = CBR
        encoder.set_property("bitrate", 6_000_000)
        encoder.set_property("iframeinterval", 15)       # keyframe every ~1s at 15 fps
        encoder.set_property("insert-sps-pps", True)
        encoder.set_property("preset-level", 1)

        parser = self._make_element("h264parse", f"webrtc-parse-{session.id}")
        parser.set_property("config-interval", -1)       # push SPS/PPS with every IDR
        try:
            # force conversion if upstream might already be H.264; avoids passthrough byte-stream
            parser.set_property("disable-passthrough", True)
        except TypeError:
            pass  # older GStreamer

        # Enforce WebRTC-friendly caps AFTER parse: AVCC + AU
        h264_caps = self._make_element("capsfilter", f"webrtc-h264caps-{session.id}")
        # (profile/level are negotiable; keep minimal strictness)
        h264_caps.set_property(
            "caps",
            Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au")
        )

        payloader = self._make_element("rtph264pay", f"webrtc-pay-{session.id}")
        payloader.set_property("config-interval", 1)     # emit SPS/PPS regularly in RTP
        # Leave PT to negotiation; we set it later if we parse it from the offer

        webrtcbin = self._make_element("webrtcbin", f"webrtcbin-{session.id}")
        webrtcbin.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        webrtcbin.set_property("stun-server", "stun://stun.l.google.com:19302")

        # Signals: ICE + PC state (and we use these to trigger FKU elsewhere)
        webrtcbin.connect("on-ice-candidate", self._on_ice_candidate, session.id)
        webrtcbin.connect("notify::ice-connection-state", self._on_ice_connection_state_notify, session.id)
        webrtcbin.connect("notify::ice-gathering-state", self._on_ice_gathering_state_notify, session.id)
        webrtcbin.connect("notify::connection-state", self._on_connection_state_notify, session.id)

        elements = [queue, converter, caps_in, encoder, parser, h264_caps, payloader, webrtcbin]
        for e in elements:
            self._pipeline.add(e)

        # --- Linking --------------------------------------------------------------
        tee_pad: Optional[Gst.Pad] = None
        request_pads: list[Tuple[Gst.Element, Gst.Pad]] = []

        try:
            # Upstream → encoder chain
            tee_pad = self._link_tee_to_queue(self._tee, queue)
            if not queue.link(converter):
                raise RuntimeError("Failed to link WebRTC queue → converter")
            if not converter.link(caps_in):
                raise RuntimeError("Failed to link WebRTC converter → caps_in")
            if not caps_in.link(encoder):
                raise RuntimeError("Failed to link WebRTC caps_in → encoder")
            if not encoder.link(parser):
                raise RuntimeError("Failed to link WebRTC encoder → parser")
            if not parser.link(h264_caps):
                raise RuntimeError("Failed to link WebRTC parser → h264_caps")
            if not h264_caps.link(payloader):
                raise RuntimeError("Failed to link WebRTC h264_caps → payloader")

            # Payloader → webrtcbin: request a send sink pad; this implicitly creates a SENDONLY transceiver.
            pay_src = payloader.get_static_pad("src")
            if pay_src is None:
                raise RuntimeError("Failed to obtain payloader src pad")

            webrtc_sink_pad = webrtcbin.get_request_pad("sink_%u")
            if webrtc_sink_pad is None:
                raise RuntimeError("Failed to request sink pad on webrtcbin")
            request_pads.append((webrtcbin, webrtc_sink_pad))

            if pay_src.link(webrtc_sink_pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError("Failed to link payloader → webrtcbin")

        except Exception:
            # Rollback request pads and elements
            for elem, pad in request_pads:
                if pad and elem:
                    try:
                        elem.release_request_pad(pad)
                    except Exception:
                        LOGGER.exception("Failed to release request pad during WebRTC branch setup rollback")
            request_pads.clear()
            if tee_pad is not None:
                parent = tee_pad.get_parent_element()
                if parent:
                    parent.release_request_pad(tee_pad)
                if tee_pad in self._tee_src_pads:
                    self._tee_src_pads.remove(tee_pad)
            for e in elements:
                try:
                    self._pipeline.remove(e)
                except Exception:
                    LOGGER.exception("Failed to remove partially-built WebRTC element")
            raise

        # Sync states
        for e in elements:
            e.sync_state_with_parent()

        assert tee_pad is not None

        # --- Sanity: we should have exactly one transceiver (created by linking sink_%u)
        try:
            trs = webrtcbin.emit("get-transceivers") or []
            LOGGER.info("WebRTC session %s: webrtcbin has %d transceiver(s)", session.id, len(trs))
            if len(trs) == 0:
                LOGGER.warning("WebRTC session %s: no transceiver created; check sink_%%u linking.", session.id)
            elif len(trs) > 1:
                # This is the situation that leads to "Could not intersect offer direction..."
                # We don’t force-remove here (API doesn’t expose that safely); log loudly so caller can fix upstream.
                LOGGER.warning(
                    "WebRTC session %s: multiple transceivers detected (%d). "
                    "Do NOT call add-transceiver() AND sink_%%u; client must offer a single recvonly m-line.",
                    session.id, len(trs)
                )
        except Exception:
            LOGGER.debug("WebRTC session %s: get-transceivers failed (older GStreamer?)", session.id, exc_info=True)

        # --- Polling workaround (older GStreamer that sometimes misses notifies)
        def poll_ice_state(session_id: str, webrtcbin_elem: Gst.Element) -> bool:
            branch = self._webrtc_branches.get(session_id)
            if not branch:
                return False
            try:
                ice_state = webrtcbin_elem.get_property("ice-connection-state")
                conn_state = webrtcbin_elem.get_property("connection-state")
                if not hasattr(branch, "_last_ice_state"):
                    branch._last_ice_state = None
                    branch._last_conn_state = None
                if ice_state != branch._last_ice_state:
                    name = getattr(ice_state, "value_nick", str(ice_state))
                    LOGGER.info("WebRTC session %s ICE connection state: %s (polled)", session_id, name)
                    branch._last_ice_state = ice_state
                if conn_state != branch._last_conn_state:
                    name = getattr(conn_state, "value_nick", str(conn_state))
                    LOGGER.info("WebRTC session %s peer connection state: %s (polled)", session_id, name)
                    branch._last_conn_state = conn_state
            except Exception:
                LOGGER.debug("Failed to poll WebRTC state for session %s", session_id, exc_info=True)
            return True

        poll_source_id = GLib.timeout_add(500, poll_ice_state, session.id, webrtcbin)

        return _WebRTCBranch(
            session=session,
            tee_pad=tee_pad,
            elements=elements,
            request_pads=request_pads,
            webrtcbin=webrtcbin,
            drain_source_id=None,
            poll_source_id=poll_source_id,
        )

    def _ensure_mjpeg_branch(self) -> None:
        if self._mjpeg_server is None or self._pipeline is None or self._tee is None:
            return
        if self._mjpeg_branch is not None:
            return
        queue = self._make_element("queue", "mjpeg-queue")
        queue.set_property("leaky", 2)
        queue.set_property("max-size-buffers", 1)

        converter = self._make_element("nvvideoconvert", "mjpeg-converter")
        videorate = self._make_element("videorate", "mjpeg-videorate")
        capsfilter = self._make_element("capsfilter", "mjpeg-caps")
        capsfilter.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,format=I420,framerate={self._config.web.mjpeg_framerate}/1"
            ),
        )
        try:
            encoder = self._make_element("nvjpegenc", "mjpeg-encoder")
        except RuntimeError:
            encoder = self._make_element("jpegenc", "mjpeg-encoder")
        try:
            encoder.set_property("quality", 85)
        except Exception:
            LOGGER.debug("JPEG encoder quality property unsupported; using defaults")
        jpeg_caps = self._make_element("capsfilter", "mjpeg-jpeg-caps")
        jpeg_caps.set_property(
            "caps",
            Gst.Caps.from_string("image/jpeg,framerate=%d/1" % self._config.web.mjpeg_framerate),
        )
        appsink = self._make_element("appsink", "mjpeg-appsink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.set_property("drop", True)
        appsink.set_property("max-buffers", 1)
        appsink.set_property("caps", Gst.Caps.from_string("image/jpeg"))

        elements = [queue, converter, videorate, capsfilter, encoder, jpeg_caps, appsink]
        for element in elements:
            self._pipeline.add(element)

        tee_pad: Optional[Gst.Pad] = None
        signal_id: Optional[int] = None

        try:
            tee_pad = self._link_tee_to_queue(self._tee, queue)
            for upstream, downstream in zip(elements, elements[1:]):
                if not upstream.link(downstream):
                    raise RuntimeError(
                        f"Failed to link MJPEG branch element {upstream.get_name()} to {downstream.get_name()}"
                    )
            signal_id = appsink.connect("new-sample", self._on_mjpeg_sample)
        except Exception:
            LOGGER.exception("Failed to initialise MJPEG fallback branch")
            if signal_id is not None:
                appsink.disconnect(signal_id)
            if tee_pad is not None:
                parent = tee_pad.get_parent_element()
                if parent:
                    parent.release_request_pad(tee_pad)
                if tee_pad in self._tee_src_pads:
                    self._tee_src_pads.remove(tee_pad)
            for element in elements:
                try:
                    self._pipeline.remove(element)
                except Exception:
                    LOGGER.debug("Failed to remove MJPEG element during rollback", exc_info=True)
            return

        for element in elements:
            element.sync_state_with_parent()

        assert tee_pad is not None and signal_id is not None
        self._mjpeg_branch = _MjpegBranch(tee_pad=tee_pad, elements=elements, appsink=appsink, signal_id=signal_id)
        LOGGER.info("MJPEG fallback branch initialised")

    def _on_mjpeg_sample(self, appsink) -> Gst.FlowReturn:
        if self._mjpeg_server is None:
            return Gst.FlowReturn.OK
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        if buffer is None:
            return Gst.FlowReturn.ERROR
        buffer_size = buffer.get_size()
        if buffer_size <= 0:
            return Gst.FlowReturn.OK
        try:
            frame_bytes = buffer.extract_dup(0, buffer_size)
        except Exception:
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                return Gst.FlowReturn.ERROR
            try:
                frame_bytes = bytes(map_info.data)
            finally:
                buffer.unmap(map_info)
        try:
            self._mjpeg_server.publish_frame(frame_bytes)
            if not self._mjpeg_logged_first_frame:
                self._mjpeg_logged_first_frame = True
                LOGGER.info("MJPEG fallback publishing frames (%d bytes)", len(frame_bytes))
        except Exception:
            LOGGER.debug("Failed to publish MJPEG frame", exc_info=True)
        return Gst.FlowReturn.OK

    def _teardown_mjpeg_branch(self) -> None:
        if self._mjpeg_branch is None:
            return
        branch = self._mjpeg_branch
        self._mjpeg_branch = None
        try:
            if branch.signal_id is not None:
                branch.appsink.disconnect(branch.signal_id)
        except Exception:
            LOGGER.debug("Failed to disconnect MJPEG appsink callback", exc_info=True)
        if branch.tee_pad in self._tee_src_pads:
            self._tee_src_pads.remove(branch.tee_pad)
        parent = branch.tee_pad.get_parent_element()
        if parent:
            try:
                parent.release_request_pad(branch.tee_pad)
            except Exception:
                LOGGER.debug("Failed to release MJPEG tee pad", exc_info=True)
        for element in branch.elements:
            try:
                element.set_state(Gst.State.NULL)
            except Exception:
                LOGGER.debug("Failed to set MJPEG element to NULL", exc_info=True)
            try:
                if self._pipeline is not None:
                    self._pipeline.remove(element)
            except Exception:
                LOGGER.debug("Failed to remove MJPEG element from pipeline", exc_info=True)
        LOGGER.info("MJPEG fallback branch torn down")

    def _drain_session_messages(self, session_id: str) -> bool:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return False
        session = branch.session
        for message in session.drain_browser_messages():
            try:
                msg_type = message.get("type")
                if msg_type == "offer":
                    self._handle_session_offer(branch, message)
                elif msg_type == "candidate":
                    self._handle_session_candidate(branch, message)
            except Exception:
                LOGGER.exception("Failed to handle signaling message for session %s", session_id)
        if session.is_closed():
            branch.drain_source_id = None
            return False
        return True

    def _handle_session_offer(self, branch: _WebRTCBranch, message: dict) -> None:
        sdp_text = message.get("sdp")
        if not sdp_text:
            LOGGER.warning("Offer without SDP for session %s", branch.session.id)
            return
        LOGGER.info("Received SDP offer for session %s", branch.session.id)
        result, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        if result != GstSdp.SDPResult.OK:
            LOGGER.error("Invalid SDP offer for session %s: %s", branch.session.id, result)
            return
        h264_pt = self._extract_h264_payload_type(sdp)
        if h264_pt is not None and self._pipeline is not None:
            payloader = self._pipeline.get_by_name(f"webrtc-pay-{branch.session.id}")
            if payloader:
                try:
                    payloader.set_property("pt", int(h264_pt))
                    LOGGER.debug(
                        "Configured payloader PT=%s for WebRTC session %s (from offer)",
                        h264_pt,
                        branch.session.id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed to set payloader PT for WebRTC session %s", branch.session.id
                    )

        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp
        )
        promise = Gst.Promise.new_with_change_func(
            self._on_remote_description_set, branch.session.id
        )
        branch.webrtcbin.emit("set-remote-description", offer, promise)

    def _handle_session_candidate(self, branch: _WebRTCBranch, message: dict) -> None:
        candidate_info = message.get("candidate") or {}
        candidate_str = candidate_info.get("candidate")
        if not candidate_str:
            LOGGER.debug("Skipping empty ICE candidate for session %s", branch.session.id)
            return
        mline_index = int(candidate_info.get("sdpMLineIndex", 0))

        # Parse candidate to extract useful info for debugging
        candidate_parts = candidate_str.split()
        candidate_type = "unknown"
        ip_address = "unknown"

        if len(candidate_parts) >= 5:
            ip_address = candidate_parts[4]  # IP is typically the 5th element
        if "typ" in candidate_parts:
            typ_idx = candidate_parts.index("typ")
            if typ_idx + 1 < len(candidate_parts):
                candidate_type = candidate_parts[typ_idx + 1]  # host/srflx/relay

        # Check for mDNS candidates
        is_mdns = ".local" in ip_address
        if is_mdns:
            LOGGER.warning(
                "WebRTC session %s received mDNS candidate from client: type=%s ip=%s - "
                "Server cannot resolve .local addresses! This will prevent connection. "
                "Client should disable chrome://flags/#enable-webrtc-hide-local-ips-with-mdns",
                branch.session.id, candidate_type, ip_address
            )
        else:
            LOGGER.info(
                "WebRTC session %s received ICE candidate from client: type=%s ip=%s mline=%s",
                branch.session.id, candidate_type, ip_address, mline_index
            )

        LOGGER.debug("WebRTC session %s full remote candidate: %s", branch.session.id, candidate_str)
        branch.webrtcbin.emit("add-ice-candidate", mline_index, candidate_str)

    def _on_remote_description_set(self, promise: Gst.Promise, session_id: str) -> None:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return
        new_promise = Gst.Promise.new_with_change_func(self._on_answer_created, session_id)
        branch.webrtcbin.emit("create-answer", None, new_promise)

    def _on_answer_created(self, promise: Gst.Promise, session_id: str) -> None:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return
        reply = promise.get_reply()
        if reply is None or not reply.has_field("answer"):
            LOGGER.error("Answer creation failed for session %s", session_id)
            branch.session.close("answer-failed")
            return
        answer: GstWebRTC.WebRTCSessionDescription = reply.get_value("answer")
        branch.webrtcbin.emit("set-local-description", answer, Gst.Promise.new())
        sdp_text = answer.sdp.as_text()
        LOGGER.info("Sending SDP answer to session %s", session_id)
        branch.session.send_to_browser({"type": "answer", "sdp": sdp_text})
        try:
            branch.webrtcbin.emit("gather-candidates")
        except TypeError:
            LOGGER.debug(
                "webrtcbin gather-candidates signal unavailable; relying on automatic ICE gathering"
            )

    def _on_ice_candidate(self, webrtcbin, mlineindex, candidate, session_id: str) -> None:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return

        # Parse candidate to extract useful info for debugging
        candidate_parts = candidate.split()
        candidate_type = "unknown"
        ip_address = "unknown"

        if len(candidate_parts) >= 5:
            ip_address = candidate_parts[4]  # IP is typically the 5th element
        if "typ" in candidate_parts:
            typ_idx = candidate_parts.index("typ")
            if typ_idx + 1 < len(candidate_parts):
                candidate_type = candidate_parts[typ_idx + 1]  # host/srflx/relay

        # Filter out IPv6 link-local candidates (fe80::/10) and Docker bridge candidates
        # These cannot be reached by remote browsers and cause ICE to fail
        is_ipv6_link_local = ip_address.startswith("fe80:")
        is_docker_bridge = ip_address == "172.17.0.1"

        if is_ipv6_link_local:
            LOGGER.debug(
                "WebRTC session %s SKIPPING IPv6 link-local ICE candidate: type=%s ip=%s mline=%s (unreachable by browser)",
                session_id, candidate_type, ip_address, mlineindex
            )
            return

        if is_docker_bridge:
            LOGGER.debug(
                "WebRTC session %s SKIPPING Docker bridge ICE candidate: type=%s ip=%s mline=%s (unreachable by browser)",
                session_id, candidate_type, ip_address, mlineindex
            )
            return

        LOGGER.info(
            "WebRTC session %s sending ICE candidate: type=%s ip=%s mline=%s",
            session_id, candidate_type, ip_address, mlineindex
        )
        LOGGER.debug("WebRTC session %s full candidate: %s", session_id, candidate)

        branch.session.send_to_browser(
            {
                "type": "candidate",
                "candidate": {
                    "candidate": candidate,
                    "sdpMLineIndex": int(mlineindex),
                    "sdpMid": str(mlineindex),
                },
            }
        )

    def _on_ice_connection_state_notify(self, webrtcbin, pspec, session_id: str) -> None:
        try:
            state = webrtcbin.get_property("ice-connection-state")
        except Exception:
            state = None
            
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return
        ice_state = webrtcbin.get_property("ice-connection-state")
        state_name = ice_state.value_nick if hasattr(ice_state, "value_nick") else str(ice_state)
        LOGGER.info("WebRTC session %s ICE connection state: %s", session_id, state_name)

        # Log when ICE connection fails
        if ice_state == GstWebRTC.WebRTCICEConnectionState.FAILED:
            LOGGER.error(
                "WebRTC session %s ICE connection FAILED. This often means:\n"
                "  - Client is using mDNS (.local) candidates that server can't resolve\n"
                "  - No common network path between client and server\n"
                "  - Firewall blocking UDP traffic\n"
                "  Suggestion: Disable chrome://flags/#enable-webrtc-hide-local-ips-with-mdns",
                session_id
            )
        
        # If connected, push an IDR ASAP so the browser can start decoding
        if state in (
            GstWebRTC.WebRTCICEConnectionState.CONNECTED,
            getattr(GstWebRTC.WebRTCICEConnectionState, "COMPLETED", 2),
        ):
            enc = self._pipeline.get_by_name(f"webrtc-encoder-{session_id}")
            if enc:
                sink = enc.get_static_pad("sink")
                if sink:
                    # request an immediate keyframe from upstream
                    # request an immediate keyframe from upstream
                    peer = sink.get_peer()
                    target_pad = None
                    if peer and peer.get_direction() == Gst.PadDirection.SRC:
                        target_pad = peer
                    else:
                        target_pad = sink

                    target_pad.send_event(
                        GstVideo.video_event_new_upstream_force_key_unit(
                            Gst.CLOCK_TIME_NONE,  # running-time (let GStreamer fill)
                            True,                 # all-headers
                            0                     # count
                        )
                    )

                    # belt & braces: ask again ~300ms later
                    def _again():
                        if enc:
                            s2 = enc.get_static_pad("sink")
                            if s2:
                                peer2 = s2.get_peer()
                                pad = peer2 if (peer2 and peer2.get_direction() == Gst.PadDirection.SRC) else s2
                                pad.send_event(
                                    GstVideo.video_event_new_upstream_force_key_unit(
                                        Gst.CLOCK_TIME_NONE,
                                        True,
                                        0,
                                    )
                                )
                        return False

                    GLib.timeout_add(300, _again)

    def _on_ice_gathering_state_notify(self, webrtcbin, pspec, session_id: str) -> None:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return
        gathering_state = webrtcbin.get_property("ice-gathering-state")
        state_name = gathering_state.value_nick if hasattr(gathering_state, "value_nick") else str(gathering_state)
        LOGGER.info("WebRTC session %s ICE gathering state: %s", session_id, state_name)

        # When gathering is complete, log a summary
        if gathering_state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            LOGGER.info(
                "WebRTC session %s ICE gathering complete. "
                "Server has sent all local candidates to client. "
                "Now waiting for ICE connection to establish...",
                session_id
            )

    def _on_connection_state_notify(self, webrtcbin, pspec, session_id: str) -> None:
        branch = self._webrtc_branches.get(session_id)
        if not branch:
            return
        conn_state = webrtcbin.get_property("connection-state")
        state_name = conn_state.value_nick if hasattr(conn_state, "value_nick") else str(conn_state)
        LOGGER.info("WebRTC session %s peer connection state: %s", session_id, state_name)

        # Log when connection is established successfully
        if conn_state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            LOGGER.info("WebRTC session %s successfully connected!", session_id)
        elif conn_state == GstWebRTC.WebRTCPeerConnectionState.FAILED:
            LOGGER.error("WebRTC session %s peer connection FAILED", session_id)

    def _schedule_webrtc_teardown(self, session_id: str) -> None:
        def _teardown() -> bool:
            branch = self._webrtc_branches.pop(session_id, None)
            if not branch:
                return False
            if branch.drain_source_id is not None:
                GLib.source_remove(branch.drain_source_id)
            if branch.poll_source_id is not None:
                GLib.source_remove(branch.poll_source_id)
            for element, pad in branch.request_pads:
                try:
                    element.release_request_pad(pad)
                except Exception:
                    LOGGER.exception("Failed to release WebRTC request pad during teardown")
            branch.request_pads.clear()
            if branch.tee_pad in self._tee_src_pads:
                self._tee_src_pads.remove(branch.tee_pad)
            parent = branch.tee_pad.get_parent_element()
            if parent:
                parent.release_request_pad(branch.tee_pad)
            for element in branch.elements:
                try:
                    element.set_state(Gst.State.NULL)
                except Exception:
                    LOGGER.exception("Failed to set WebRTC branch element to NULL")
                try:
                    if self._pipeline is not None:
                        self._pipeline.remove(element)
                except Exception:
                    LOGGER.exception("Failed to remove WebRTC element from pipeline")
            LOGGER.info("WebRTC session %s torn down", session_id)
            return False

        GLib.idle_add(_teardown, priority=GLib.PRIORITY_LOW)

    def _shutdown_webrtc_sessions(self, reason: str) -> None:
        for session_id, branch in list(self._webrtc_branches.items()):
            branch.session.close(reason)
            self._schedule_webrtc_teardown(session_id)

    @staticmethod
    def _extract_h264_payload_type(sdp: GstSdp.SDPMessage) -> Optional[int]:
        """Return the payload type advertised for H264 in the remote SDP (if any)."""
        media_index = 0
        while True:
            try:
                media = sdp.get_media(media_index)
            except (IndexError, ValueError):
                break
            try:
                media_name = media.get_media().lower()
            except Exception:
                media_index += 1
                continue
            if media_name != "video":
                media_index += 1
                continue
            try:
                attr_len = media.attributes_len()
            except Exception:
                attr_len = 0
            for attr_index in range(attr_len):
                try:
                    attr = media.get_attribute(attr_index)
                except Exception:
                    continue
                if not attr:
                    continue
                key = getattr(attr, "key", "")
                if not isinstance(key, str) or key.lower() != "rtpmap":
                    continue
                value = getattr(attr, "value", "")
                if not value:
                    continue
                parts = value.split(None, 1)
                if len(parts) != 2:
                    continue
                pt_str, codec = parts
                if "H264/90000" not in codec.upper():
                    continue
                try:
                    return int(pt_str)
                except ValueError:
                    continue
            media_index += 1
        return None


__all__ = ["DeepStreamPipeline"]

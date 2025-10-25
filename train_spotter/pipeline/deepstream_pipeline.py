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
gi.require_version("GstVideo", "1.0")

from gi.repository import GLib, GObject, Gst, GstVideo  # type: ignore

from train_spotter.pipeline.analytics import StreamAnalytics, analytics_pad_probe
from train_spotter.service.config import AppConfig
from train_spotter.service.roi import ROIConfig
from train_spotter.storage import EventBus, EventMessage, EventType

if TYPE_CHECKING:
    from train_spotter.web.mjpeg import MJPEGStreamServer

LOGGER = logging.getLogger(__name__)


@dataclass
class _MjpegBranch:
    tee_pad: Gst.Pad
    elements: list[Gst.Element]
    appsink: Gst.Element
    signal_id: Optional[int]


@dataclass
class _RtspBranch:
    tee_pad: Gst.Pad
    elements: list[Gst.Element]
    rtsp_sink: Gst.Element


class DeepStreamPipeline:
    """Build and control the DeepStream pipeline according to application config."""

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        roi_config: ROIConfig | None = None,
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
        self._tee: Optional[Gst.Element] = None
        Gst.init(None)
        self._mjpeg_server = mjpeg_server
        self._mjpeg_branch: Optional[_MjpegBranch] = None
        self._mjpeg_logged_first_frame = False
        self._rtsp_branch: Optional[_RtspBranch] = None

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
        self._ensure_rtsp_branch()

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
        self._teardown_mjpeg_branch()
        self._teardown_rtsp_branch()

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

    def _ensure_rtsp_branch(self) -> None:
        """Create permanent RTSP output branch for MediaMTX bridge."""
        if not self._config.web.enable_rtsp_output or self._pipeline is None or self._tee is None:
            return
        if self._rtsp_branch is not None:
            return

        LOGGER.info("Creating UDP/MPEG-TS output branch for MediaMTX")

        # Build pipeline: tee → queue → nvvidconv → nvv4l2h264enc → h264parse → mpegtsmux → udpsink
        queue = self._make_element("queue", "udp-queue")
        queue.set_property("leaky", 2)
        queue.set_property("max-size-buffers", 4)

        # Video conversion for encoder
        converter = self._make_element("nvvideoconvert", "udp-converter")
        capsfilter = self._make_element("capsfilter", "udp-caps")
        capsfilter.set_property(
            "caps",
            Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12")
        )

        # H.264 encoding
        encoder = self._make_element("nvv4l2h264enc", "udp-encoder")
        encoder.set_property("bitrate", 2000000)  # 2 Mbps
        encoder.set_property("preset-level", 1)  # UltraFastPreset
        encoder.set_property("insert-sps-pps", True)
        encoder.set_property("insert-vui", True)

        parser = self._make_element("h264parse", "udp-h264parse")

        # MPEG-TS muxer
        muxer = self._make_element("mpegtsmux", "udp-mpegtsmux")
        muxer.set_property("alignment", 7)  # 7 packets per buffer for UDP streaming

        # UDP sink to MediaMTX
        udp_sink = self._make_element("udpsink", "udp-sink")
        udp_sink.set_property("host", "127.0.0.1")
        udp_sink.set_property("port", 5600)  # MediaMTX expects stream on this port
        udp_sink.set_property("sync", False)  # Don't sync to clock for lower latency

        elements = [queue, converter, capsfilter, encoder, parser, muxer, udp_sink]
        for element in elements:
            self._pipeline.add(element)

        tee_pad: Optional[Gst.Pad] = None

        try:
            tee_pad = self._link_tee_to_queue(self._tee, queue)
            for upstream, downstream in zip(elements, elements[1:]):
                if not upstream.link(downstream):
                    raise RuntimeError(
                        f"Failed to link UDP/MPEG-TS branch element {upstream.get_name()} to {downstream.get_name()}"
                    )
        except Exception:
            LOGGER.exception("Failed to initialise UDP/MPEG-TS output branch")
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
                    LOGGER.debug("Failed to remove RTSP element during rollback", exc_info=True)
            return

        for element in elements:
            element.sync_state_with_parent()

        assert tee_pad is not None
        self._rtsp_branch = _RtspBranch(tee_pad=tee_pad, elements=elements, rtsp_sink=udp_sink)
        LOGGER.info("UDP/MPEG-TS output branch initialised - sending to MediaMTX on 127.0.0.1:5600")

    def _teardown_rtsp_branch(self) -> None:
        """Tear down RTSP output branch."""
        if self._rtsp_branch is None:
            return
        branch = self._rtsp_branch
        self._rtsp_branch = None

        if branch.tee_pad in self._tee_src_pads:
            self._tee_src_pads.remove(branch.tee_pad)
        parent = branch.tee_pad.get_parent_element()
        if parent:
            try:
                parent.release_request_pad(branch.tee_pad)
            except Exception:
                LOGGER.debug("Failed to release RTSP tee pad", exc_info=True)

        for element in branch.elements:
            try:
                element.set_state(Gst.State.NULL)
            except Exception:
                LOGGER.debug("Failed to set RTSP element to NULL", exc_info=True)
            try:
                if self._pipeline is not None:
                    self._pipeline.remove(element)
            except Exception:
                LOGGER.debug("Failed to remove RTSP element from pipeline", exc_info=True)
        LOGGER.info("RTSP output branch torn down")

__all__ = ["DeepStreamPipeline"]

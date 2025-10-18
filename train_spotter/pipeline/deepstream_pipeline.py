"""DeepStream pipeline scaffolding for train spotter application."""

from __future__ import annotations

import configparser
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GObject", "2.0")

from gi.repository import GLib, GObject, Gst  # type: ignore

from train_spotter.pipeline.analytics import StreamAnalytics, analytics_pad_probe
from train_spotter.service.config import AppConfig
from train_spotter.service.roi import ROIConfig
from train_spotter.storage import EventBus, EventMessage, EventType

LOGGER = logging.getLogger(__name__)


class DeepStreamPipeline:
    """Build and control the DeepStream pipeline according to application config."""

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        overlay_controller=None,
        roi_config: ROIConfig | None = None,
        frame_callback: Optional[Callable[[bytes], None]] = None,
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
            StreamAnalytics(config, event_bus, overlay_controller, roi_config)
            if enable_inference
            else None
        )
        self._frame_callback = frame_callback
        self._tee_src_pads: list[Gst.Pad] = []
        self._appsink: Optional[Gst.Element] = None
        Gst.init(None)

    def build(self) -> None:
        LOGGER.info("Building DeepStream pipeline")
        self._pipeline = Gst.Pipeline.new("train_spotter_pipeline")
        if self._pipeline is None:
            raise RuntimeError("Failed to create DeepStream pipeline")

        source_bin = self._create_source_bin(self._config.camera_source)
        streammux = self._make_element("nvstreammux", "stream-muxer")
        streammux.set_property("batch-size", 1)
        streammux.set_property("width", 640)
        streammux.set_property("height", 480)
        streammux.set_property("live-source", 1)
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

        tee = self._make_element("tee", "display-tee")
        queue_display = self._make_element("queue", "display-queue")
        queue_display.set_property("leaky", 2)
        queue_display.set_property("max-size-buffers", 4)
        sink = self._make_element(self._config.display.sink_type, "display-sink")
        sink.set_property("sync", False)

        queue_web = self._make_element("queue", "web-queue")
        queue_web.set_property("leaky", 2)
        queue_web.set_property("max-size-buffers", 2)
        nvjpegenc = self._make_element("nvjpegenc", "jpeg-encoder")
        nvjpegenc.set_property("quality", 70)
        appsink = self._make_element("appsink", "web-appsink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.connect("new-sample", self._on_new_sample)
        self._appsink = appsink

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
                queue_display,
                sink,
                queue_web,
                nvjpegenc,
                appsink,
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
        self._link_tee_to_queue(tee, queue_display)
        if not queue_display.link(sink):
            raise RuntimeError("Failed to link display queue to sink")
        self._link_tee_to_queue(tee, queue_web)
        if not queue_web.link(nvjpegenc):
            raise RuntimeError("Failed to link web queue to nvjpegenc")
        if not nvjpegenc.link(appsink):
            raise RuntimeError("Failed to link nvjpegenc to appsink")

        if self._enable_inference and self._analytics is not None:
            tracker_src_pad = tracker.get_static_pad("src") if tracker is not None else None
            if tracker_src_pad:
                tracker_src_pad.add_probe(
                    Gst.PadProbeType.BUFFER, analytics_pad_probe, self._analytics
                )
            else:
                LOGGER.warning("Failed to attach analytics probe; tracker src pad missing")

    def start(self) -> None:
        if self._pipeline is None:
            self.build()
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

    def stop(self) -> None:
        if self._pipeline is None:
            return
        LOGGER.info("Stopping DeepStream pipeline")
        self._pipeline.set_state(Gst.State.NULL)
        if self._main_loop:
            self._main_loop.quit()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
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
        bin_desc = f"{source_desc} ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! queue"
        source_bin = Gst.parse_bin_from_description(bin_desc, True)
        source_bin.set_name("source-bin")
        return source_bin

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

    def _link_tee_to_queue(self, tee, queue):
        srcpad = tee.get_request_pad("src_%u")
        if not srcpad:
            raise RuntimeError("Failed to request src pad from tee")
        sinkpad = queue.get_static_pad("sink")
        if not sinkpad:
            raise RuntimeError("Queue missing sink pad")
        if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link tee to queue")
        self._tee_src_pads.append(srcpad)

    def _on_new_sample(self, sink):
        if not self._frame_callback:
            return Gst.FlowReturn.OK
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        if buffer is None:
            return Gst.FlowReturn.OK
        success, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK
        try:
            data = bytes(mapinfo.data)
            self._frame_callback(data)
        finally:
            buffer.unmap(mapinfo)
        return Gst.FlowReturn.OK


__all__ = ["DeepStreamPipeline"]

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Train Spotter is a Jetson Xavier AGX application that uses NVIDIA DeepStream to analyze rail-and-road scenes, detect trains, track vehicles, and expose a web dashboard with live WebRTC/MJPEG streaming.

## Development Commands

### Running the Application

```bash
# Full pipeline with camera/video source
python -m train_spotter.service.main --config configs/traffic_video.json

# Web dashboard only (expects external pipeline)
python -m train_spotter.service.main --web-only

# Passthrough mode (skip inference, raw camera feed)
python -m train_spotter.service.main --config configs/usb_camera.json --passthrough

# Enable GStreamer debug logging
python -m train_spotter.service.main --config configs/traffic_video.json --gst-debug 3
```

### Testing

```bash
# Run all tests
python -m pytest

# Run specific test module
python -m pytest train_spotter/tests/test_config.py

# Run with verbose output
python -m pytest -v
```

### Setting Up v4l2loopback for Testing

```bash
# Load loopback module (requires sudo)
sudo modprobe v4l2loopback video_nr=10 card_label=TrafficLoopback exclusive_caps=1

# Stream test video to loopback device
python tools/v4l2_loopback_player.py train_spotter/tests/traffic.mp4 --device /dev/video10

# Run application with loopback source
python -m train_spotter.service.main --config configs/traffic_video.json
```

## Architecture

### Component Hierarchy

The application follows a layered architecture:

1. **Entry Point** (`train_spotter.service.main`):
   - Orchestrates all components: pipeline, database, event bus, web server, signaling server
   - `EventProcessor` subscribes to event bus and persists events to database
   - Runs DeepStream pipeline, web dashboard, WebRTC signaling server, and MJPEG server in separate threads

2. **DeepStream Pipeline** (`train_spotter.pipeline.deepstream_pipeline`):
   - Dynamically builds GStreamer pipeline with NVIDIA hardware acceleration elements
   - Supports camera sources (CSI/USB via `nvarguscamerasrc`, v4l2src) or file playback
   - Pipeline flow: source → nvstreammux → nvinfer (YOLO11) → nvtracker → nvvideoconvert → nvdsosd → tee
   - Tee splits output to: fakesink (null output), dynamically-attached WebRTC branches per client, and MJPEG fallback stream
   - WebRTC branches are created/torn down per-session using GLib idle callbacks
   - Runs GLib main loop in dedicated thread to process GStreamer bus messages

3. **Analytics** (`train_spotter.pipeline.analytics`):
   - `StreamAnalytics.process_frame()` is invoked via pad probe on tracker src pad
   - Extracts `DetectedObject` instances from DeepStream `NvDsBatchMeta`
   - `TrainStateMachine` uses coverage-based heuristics with hit/miss thresholds to emit train events
   - `VehicleTrackerHooks` assigns vehicles to lanes via point-in-polygon tests and emits vehicle events when tracks go stale

4. **Event Bus** (`train_spotter.storage.event_bus`):
   - Thread-safe publish/subscribe queue for `EventMessage` (TRAIN_STARTED, TRAIN_ENDED, VEHICLE_EVENT, HEARTBEAT)
   - Subscribers receive all published events; gracefully handles full queues by dropping oldest

5. **Storage** (`train_spotter.storage.db`):
   - SQLite with WAL mode for concurrent reads
   - Pydantic models: `TrainEvent`, `VehicleEvent`
   - Thread-safe via RLock; all writes use transactions

6. **Web Dashboard** (`train_spotter.web.app`):
   - Flask app serving templates and API endpoints (`/`, `/history`, `/api/status`)
   - WebRTC signaling is handled by standalone `WebRTCSignalingServer` (WebSocket server on port 8765)
   - MJPEG fallback streaming via `MJPEGStreamServer` (WebSocket server on port 8766)
   - Frontend uses `viewer.js` to negotiate WebRTC or fall back to MJPEG

7. **Configuration** (`train_spotter.service.config`):
   - Pydantic models for type-safe configuration loading from JSON
   - `AppConfig.from_file()` loads from path; `resolve_config()` provides defaults
   - ROI configuration is separate: `train_spotter.service.roi.load_roi_config()`

### Critical Integration Points

- **Analytics ↔ Pipeline**: `analytics_pad_probe` attached to tracker src pad processes each frame's metadata
- **Analytics ↔ Event Bus**: `StreamAnalytics` publishes domain events (train/vehicle) extracted from DeepStream metadata
- **Event Bus ↔ Database**: `EventProcessor` thread consumes events and writes to SQLite
- **Pipeline ↔ WebRTC**: `WebRTCManager` requests sessions; pipeline dynamically adds/removes GStreamer branches with `webrtcbin`, encoder, and payloader
- **Pipeline ↔ MJPEG**: Permanent branch with `appsink`; `_on_mjpeg_sample` callback publishes JPEG frames to `MJPEGStreamServer`

### DeepStream-Specific Details

- **Source Flexibility**: Pipeline accepts GStreamer pipeline strings in `camera_source` config. If `file://` or `filesrc location=` is detected, builds file source bin with `qtdemux`, `h264parse`, `nvv4l2decoder`. Otherwise, wraps as camera source bin (e.g., `nvarguscamerasrc ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! queue`).
- **Tracker Configuration**: `_configure_tracker()` reads INI-style config and sets properties dynamically. Relative paths in `ll-lib-file`/`ll-config-file` are resolved relative to config directory.
- **WebRTC Branch Lifecycle**: Created via `_create_webrtc_branch()` when session is requested. Elements added to pipeline, synced with parent state. Torn down via `_schedule_webrtc_teardown()` when session closes. All GStreamer operations are scheduled via `GLib.idle_add()` to ensure they run in the main loop thread.
- **MJPEG Fallback**: `_ensure_mjpeg_branch()` creates permanent branch during `build()`. Uses `nvjpegenc` if available, falls back to software `jpegenc`. `appsink` emits signals for each frame; callback publishes to WebSocket server.

## Key Configuration Files

- `configs/traffic_video.json`: Pre-configured for v4l2loopback (`/dev/video10`) with traffic.mp4
- `configs/usb_camera.json`: USB webcam via v4l2src
- `configs/v4l2loopback_camera.json`: Generic v4l2 loopback setup
- `configs/trafficcamnet_yolo11.txt`: nvinfer config for YOLO11n model
- `configs/iou_tracker_config.txt`: IOU tracker settings for road scenes
- `train_spotter/data/roi_config.json`: Polygon definitions for train detection zone and vehicle lanes

## Environment Requirements

- **Platform**: NVIDIA Jetson Xavier AGX (or similar with DeepStream support)
- **JetPack**: Installed with DeepStream SDK and `pyds` Python bindings
- **Python**: 3.11.4 (via pyenv) with venv
- **GStreamer**: 1.16.3 (from JetPack) - see WebRTC limitations below
- **GStreamer Plugins**: `gstreamer1.0-nice` required for WebRTC; `nvjpegenc` preferred for MJPEG
- **Camera**: CSI camera accessible via `nvarguscamerasrc` or USB camera via v4l2src

### GStreamer 1.16 WebRTC Limitations

The Jetson Xavier AGX runs GStreamer 1.16.3 (bundled with JetPack/DeepStream). This version has significant limitations with the `webrtcbin` element:

**Known Issues:**

1. **No Transceiver Direction Control**: GStreamer 1.16's `webrtcbin` does not support setting transceiver direction programmatically. The `set_direction()` method and `direction` property are not available on transceiver objects.

2. **Automatic Transceiver Creation**: When linking a payloader to `webrtcbin.sink_%u`, it automatically creates a **RECVONLY** transceiver instead of SENDONLY, even though we're sending video to the browser.

3. **SDP Direction Mismatch**: The auto-generated SDP answer contains `a=recvonly` (server wants to receive), which doesn't intersect with the browser's `a=recvonly` offer, causing media rejection (`m=video 0` port rejection).

4. **Limited WebRTC API**: Features available in GStreamer >= 1.18 are not available:
   - Transceiver codec preferences
   - Manual transceiver creation with specific directions
   - `on-new-transceiver` signal handlers with direction modification

**Attempted Workarounds (Unsuccessful):**

- ❌ Pre-creating SENDONLY transceiver with `add-transceiver` signal - Creates duplicate transceivers
- ❌ Using `on-new-transceiver` signal to modify direction - `direction` property doesn't exist in 1.16
- ❌ SDP text manipulation after answer creation - webrtcbin rejects media internally before we can fix it
- ❌ Creating new SDPMessage from fixed text and setting as local description - webrtcbin still checks internal transceiver state

**Current Working Solution:**

The application uses **per-session encoder chains** instead of a shared encoder with broadcast distribution. Each WebRTC client gets:
- Dedicated encoder pipeline: `queue → nvvideoconvert → nvv4l2h264enc → h264parse → rtph264pay → webrtcbin`
- This creates proper SENDONLY transceivers that work with GStreamer 1.16

While less efficient (multiple encoder instances), this approach is **compatible with GStreamer 1.16** and works reliably.

**Future Migration Path:**

When upgrading to GStreamer >= 1.22:
- Consider migrating to `webrtcsink` element (from gst-plugins-rs) for simplified WebRTC streaming
- Or implement shared encoder broadcast model with proper transceiver direction control
- See `WEBRTC_SIMPLIFICATION.md` (if present) for broadcast architecture design

**References:**
- GStreamer 1.16 webrtcbin docs: https://gstreamer.freedesktop.org/documentation/webrtc/webrtcbin.html
- Transceiver direction support added in GStreamer 1.18+
- `webrtcsink` requires GStreamer >= 1.22

## Testing Notes

- Some tests require NVIDIA TensorRT/PyCUDA and will be skipped on non-Jetson machines
- Use `python -m pytest train_spotter/tests` to run test suite
- `test_web_stream.py` validates WebRTC and MJPEG streaming infrastructure
- DeepStream imports wrapped in try/except to allow unit tests to run without `pyds`

## Common Patterns

- **GStreamer Element Creation**: Always use `_make_element()` which raises if factory fails
- **Pad Linking**: Check return value against `Gst.PadLinkReturn.OK`; raise `RuntimeError` on failure
- **Thread Safety**: All GStreamer state changes must occur in GLib main loop thread. Use `GLib.idle_add()` for cross-thread pipeline modifications.
- **Event Flow**: Analytics → EventBus → [EventProcessor (DB), OverlayController (UI)]
- **Logging**: Use module-level `LOGGER = logging.getLogger(__name__)`; set level via `--log-level` CLI arg

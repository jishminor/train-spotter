# Train Spotter

Train Spotter runs on NVIDIA Jetson hardware and couples a DeepStream inference pipeline with a Flask dashboard that streams low-latency video over WebRTC (MediaMTX bridge) and records train / vehicle activity.

## Quick Start
1. **Install NVIDIA stack** – JetPack / L4T with DeepStream (including the `python3-pyds` bindings) must be present on the Jetson. Ensure your USB or CSI camera is accessible via GStreamer (`v4l2src` / `nvarguscamerasrc`). Install supporting GStreamer plugins if needed (`sudo apt install gstreamer1.0-nice gstreamer1.0-plugins-good`).
2. **Clone & set up Python**
   ```bash
   git clone <repo-url>
   cd train-spotter
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. **Start MediaMTX for WebRTC** – the repo vendors a build in `tools/mediamtx/`.
   ```bash
   ./tools/start_mediamtx.sh
   ```
   The default config (`configs/mediamtx.yml`) listens on:
   - RTSP ingest: `rtsp://127.0.0.1:8554/trainspotter`
   - WebRTC WHEP endpoint: `http://<device>:8889/trainspotter/whep`
   - API / metrics: `http://<device>:9997`
4. **Launch Train Spotter** – the primary configuration we ship targets a USB camera.
   ```bash
   python -m train_spotter.service.main --config configs/usb_camera.json
   ```
   Optional flags:
   - `--web-only` – skip DeepStream, only run the dashboard (attach to an external stream).
   - `--passthrough` – stream raw camera frames without inference.
   - `--gst-debug=3` – raise GStreamer log verbosity while debugging.
5. **Open the dashboard** – visit `http://<device>:8080` from a desktop or mobile browser. The footer shows WebRTC status, transport mode, and the measured playback FPS (falls back to MJPEG if WebRTC fails).

## Requirements
- NVIDIA Jetson Xavier AGX (other Jetsons will work with matching DeepStream binaries).
- JetPack 5.x with DeepStream 6.x and `python3-pyds` installed.
- GStreamer plugins: `gstreamer1.0-nice`, `gstreamer1.0-plugins-good`, `gstreamer1.0-plugins-bad`.
- MediaMTX (bundled under `tools/mediamtx/` – replace with your build if needed).
- Python 3.10+ with the packages listed in `requirements.txt`.
- SQLite (for event storage – included with standard Python).

## Configuration Files
Configuration lives under `configs/`:

- `usb_camera.json` – USB camera via `v4l2src` (default quick-start).
- `traffic_video.json` – example setup for a v4l2loopback device fed by prerecorded footage.
- `mediamtx.yml` – MediaMTX bridge settings. Update `webrtcAllowOrigin`, `webrtcICEServers2`, or `webrtcICEHostNAT1To1IPs` if you expose the Jetson over different networks/NATs.
- `trafficcamnet_yolo11.txt` and `iou_tracker_config.txt` – DeepStream nvinfer and tracker templates.

Update camera pipelines, ROI polygons, and storage paths as needed. ROI definitions live in `train_spotter/data/roi_config.json` and can be regenerated with `tools/capture_snapshot.py` + manual editing.

## Web Dashboard & Streaming
- Web server defaults to `0.0.0.0:8080`. Change via `web.host` / `web.port` in your JSON config.
- WebRTC playback negotiates through MediaMTX. ICE servers are empty by default, so add STUN/TURN entries when clients are off-device (mobile LTE, remote Wi-Fi, etc.).
- If WebRTC negotiation fails, the UI automatically switches to the MJPEG websocket fallback. The stream card badge shows mode and measured FPS.
- The signaling websocket runs on `ws://<device>:8765` (configurable via `web.signaling_port`). Ensure firewall rules permit access.

## Operating the Pipeline
1. Verify the camera feed with GStreamer (`gst-launch-1.0 <pipeline> ! fakesink`).
2. Start MediaMTX (`./tools/start_mediamtx.sh`).
3. Run `python -m train_spotter.service.main --config configs/usb_camera.json`.
4. Watch logs for `UDP/MPEG-TS output branch initialised` (pipeline side) and `ready to serve` (MediaMTX).
5. Browse the dashboard. The sidebar shows train/vehicle counts; the live card shows connection status, transport, and FPS.

To stop, press `Ctrl+C` in the Train Spotter terminal. MediaMTX continues until you terminate it.

## Utility Scripts
- `tools/capture_snapshot.py` – capture a still for ROI tuning (`python tools/capture_snapshot.py snapshots/site.png`).
- `tools/v4l2_loopback_player.py` – loop an MP4 into a synthetic `/dev/video*` (pair with `traffic_video.json`).
- `tools/start_mediamtx.sh` – launch the bundled MediaMTX build.
- `test_webrtc_connection.py` – quick sanity check for the signaling server.

## Testing
Activate the virtual environment and run:
```bash
python -m pytest
```
Some tests expect DeepStream-specific modules; skip them on development machines without Jetson tooling.

## Troubleshooting
- **Mobile browsers fall back to MJPEG** – add reachable ICE servers in `configs/mediamtx.yml`, expose the Jetson via HTTPS (Safari requires secure origins), and ensure the encoder profile is compatible (set `nvv4l2h264enc` to Baseline if needed).
- **No WebRTC video** – confirm MediaMTX is running, `gstreamer1.0-nice` is installed, and the RTSP stream is ingesting (`mediamtx` logs show track status).
- **ROI or detection drift** – re-capture reference frames and adjust polygons in `roi_config.json`.

## Deployment
A sample systemd unit lives under `deployment/train-spotter.service`. Update paths/users, enable with `sudo systemctl enable --now train-spotter`. Consider supervising MediaMTX with its own unit.


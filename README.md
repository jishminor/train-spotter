# Train Spotter

A Jetson Xavier AGX application that uses DeepStream to analyse a rail-and-road scene, detect trains and track vehicles, record events to persistent storage, and expose both an on-device HDMI overlay and a lightweight web dashboard with live and historical views.

## High-level Features
- DeepStream-based video pipeline with CSI/USB camera support.
- Train presence detection with duration logging.
- Vehicle tracking and per-lane counts.
- HDMI overlay showing live inference results directly on the device.
- Web dashboard hosting live MJPEG stream and historical event summaries.
- SQLite-backed persistence for robust storage on performant embedded hardware.

## Repository Structure
```
train_spotter/
  pipeline/          # DeepStream pipeline assembly & analytics integration
  storage/           # SQLite persistence, event bus utilities
  ui/                # On-device display helpers
  web/               # Flask dashboard & streaming endpoints
  service/           # Application orchestration and configuration glue
  data/              # ROI definitions and static assets
  configs/           # DeepStream nvinfer/tracker configuration templates
  deployment/        # Systemd unit and deployment helpers
  tools/             # Utility scripts (e.g. ROI calibration snapshot)
```

The code base is structured to run directly on the Jetson Xavier AGX and assumes NVIDIA's DeepStream SDK is already installed (including the `pyds` Python bindings).

## Getting Started
1. Ensure JetPack and DeepStream are installed and the camera is accessible (e.g. `nvarguscamerasrc`).
2. Clone this repository onto the device.
3. Create and activate a virtual environment using the pyenv-installed Python 3.11.4:
   ```bash
   pyenv shell 3.11.4
   python -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
4. Calibrate regions of interest using `tools/capture_snapshot.py` to grab a reference frame and update `train_spotter/data/roi_config.json`.
5. Launch the application:
   ```bash
   python -m train_spotter.service.main --config path/to/config.json
   ```

### Looping prerecorded video with v4l2loopback

To drive the pipeline with the bundled traffic sample (or any MP4) while still
exposing a `/dev/video*` device, create a v4l2 loopback sink and stream the
video into it:

1. Load the loopback module (requires sudo). Adjust `video_nr` if you prefer a
   different device number (the sample config expects `/dev/video10`):
   ```bash
   sudo modprobe v4l2loopback video_nr=10 card_label=TrafficLoopback exclusive_caps=1
   ```
2. Start the feeder, which loops the clip forever. Match the `--device` and
   optional format arguments to the loopback you created:
   ```bash
   python tools/v4l2_loopback_player.py train_spotter/tests/traffic.mp4 --device /dev/video10
   ```
   By default the script outputs 640x480 @ 30 fps in I420 (`yuv420p`). Use
   `--pixel-format` or other flags if your pipeline expects something else.
3. Point the application at the loopback device using the provided
   `configs/traffic_video.json` (configured for `/dev/video10`, I420 input):
   ```bash
   python -m train_spotter.service.main --config configs/traffic_video.json
   ```

Stop the feeder with `Ctrl+C` when you are done. The loopback device persists
until you unload the module (`sudo modprobe -r v4l2loopback`).

### Web dashboard only

If you are testing against a prerecorded DeepStream pipeline or another video source, launch in dashboard-only mode:

```bash
python -m train_spotter.service.main --web-only
```

The default dashboard listens on `0.0.0.0:8080`. Adjust the host/port within the configuration file if required.

### Running tests

Install dev dependencies (after activating the `pyenv` virtualenv) and invoke pytest via the selected interpreter:

```bash
pip install -r requirements.txt
python -m pytest
```

## Deployment Aids
- `configs/trafficcamnet_yolo11.txt` – base nvinfer configuration targeting the bundled TrafficCamNet model. Adjust paths if your DeepStream installation differs.
- `configs/iou_tracker_config.txt` – IOU tracker defaults suitable for roadway scenes.
- `tools/capture_snapshot.py` – capture a camera still for ROI calibration (`python tools/capture_snapshot.py snapshots/site.png`).
- `deployment/train-spotter.service` – example systemd unit (update user, working directory, and config paths before enabling).

## Next Steps
- Refine ROI coordinates and thresholds for your specific installation.
- Harden DeepStream configuration for production (INT8 calibration, batching, etc.).
- Add alerting or metrics export if train detection feeds downstream systems.

Additional documentation will be added as components are implemented.

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
```

The code base is structured to run directly on the Jetson Xavier AGX and assumes NVIDIA's DeepStream SDK is already installed (including the `pyds` Python bindings).

## Getting Started
1. Ensure JetPack and DeepStream are installed and the camera is accessible (e.g. `nvarguscamerasrc`).
2. Clone this repository onto the device.
3. Create a Python environment with DeepStream dependencies and install Python requirements:
   ```bash
   pip install -r requirements.txt
   ```
4. Calibrate regions of interest with the provided tooling (TBD).
5. Launch the application:
   ```bash
   python -m train_spotter.service.main --config path/to/config.json
   ```

### Web dashboard only

If you are testing against a prerecorded DeepStream pipeline or another video source, launch in dashboard-only mode:

```bash
python -m train_spotter.service.main --web-only
```

The default dashboard listens on `0.0.0.0:8080`. Adjust the host/port within the configuration file if required.

Additional documentation will be added as components are implemented.

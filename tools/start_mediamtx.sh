#!/bin/bash
# Start MediaMTX server for Train Spotter WebRTC streaming

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIAMTX_DIR="$SCRIPT_DIR/mediamtx"
CONFIG_FILE="$SCRIPT_DIR/../configs/mediamtx.yml"

if [ ! -f "$MEDIAMTX_DIR/mediamtx" ]; then
    echo "Error: MediaMTX binary not found at $MEDIAMTX_DIR/mediamtx"
    echo "Please run the installation first"
    exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: MediaMTX config not found at $CONFIG_FILE"
    exit 1
fi

echo "Starting MediaMTX server..."
echo "Config: $CONFIG_FILE"
echo "RTSP input: rtsp://localhost:8554/trainspotter"
echo "WebRTC output: http://localhost:8889/trainspotter"
echo "API: http://localhost:9997"
echo ""

cd "$MEDIAMTX_DIR"
exec ./mediamtx "$CONFIG_FILE"

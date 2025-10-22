#!/bin/bash
# Test webcam settings and capture comparison images

DEVICE="/dev/video0"
OUTPUT_DIR="./webcam_tests"

mkdir -p "$OUTPUT_DIR"

echo "Testing different exposure and gain combinations..."

# Function to capture image with current settings
capture_test() {
    local name=$1
    local exposure=$2
    local gain=$3

    echo "Testing: exposure=$exposure, gain=$gain"

    # Apply settings
    v4l2-ctl -d "$DEVICE" --set-ctrl=exposure_auto=1
    v4l2-ctl -d "$DEVICE" --set-ctrl=exposure_auto_priority=0
    v4l2-ctl -d "$DEVICE" --set-ctrl=exposure_absolute=$exposure
    v4l2-ctl -d "$DEVICE" --set-ctrl=gain=$gain

    # Wait for camera to adjust
    sleep 1

    # Capture image
    ffmpeg -f v4l2 -i "$DEVICE" -frames:v 1 -y "$OUTPUT_DIR/${name}_exp${exposure}_gain${gain}.jpg" 2>&1 | grep -E "(Output|error)"

    echo "Saved: $OUTPUT_DIR/${name}_exp${exposure}_gain${gain}.jpg"
}

# Test various combinations
# Format: name exposure gain

# Low exposure, low gain (baseline)
capture_test "test1" 333 24

# Medium exposure, medium gain
capture_test "test2" 1000 64

# High exposure, medium gain
capture_test "test3" 2000 64

# Very high exposure, medium gain
capture_test "test4" 3000 64

# High exposure, high gain
capture_test "test5" 2000 128

# Maximum exposure, high gain (may be too bright/noisy)
capture_test "test6" 5000 128

echo ""
echo "Test complete! Images saved to $OUTPUT_DIR"
echo "Compare images to find optimal settings, then update optimize_webcam_night.sh"

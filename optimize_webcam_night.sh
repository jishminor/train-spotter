#!/bin/bash
# Optimize Logitech C270 webcam for night-time road monitoring
#
# IMPORTANT: The C270 works best in AUTO exposure mode with adjusted brightness/contrast.
# Manual exposure mode causes the gain to lock at 0, resulting in dark images.

DEVICE="/dev/video0"

echo "Optimizing webcam settings for low-light conditions..."

# Use AUTO exposure mode (Aperture Priority Mode = 3)
v4l2-ctl -d "$DEVICE" --set-ctrl=exposure_auto=3

# Enable auto exposure priority to let camera adjust exposure time
v4l2-ctl -d "$DEVICE" --set-ctrl=exposure_auto_priority=1

# Significantly increase brightness for night scenes (default=128, using 200)
v4l2-ctl -d "$DEVICE" --set-ctrl=brightness=200

# Double the contrast to help distinguish features in dim light (default=32, using 64)
v4l2-ctl -d "$DEVICE" --set-ctrl=contrast=64

# Keep auto white balance enabled for consistent color under varying street lights
v4l2-ctl -d "$DEVICE" --set-ctrl=white_balance_temperature_auto=1

# Keep backlight compensation enabled
v4l2-ctl -d "$DEVICE" --set-ctrl=backlight_compensation=1

echo ""
echo "Settings applied successfully!"
echo ""
echo "NOTE: The camera needs 3-5 seconds to adjust exposure in low light."
echo "If using ffmpeg to capture, add a delay: ffmpeg -f v4l2 -i /dev/video0 -ss 00:00:03 ..."
echo ""
echo "Current configuration:"
v4l2-ctl -d "$DEVICE" --list-ctrls

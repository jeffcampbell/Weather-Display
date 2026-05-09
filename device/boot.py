# Disable USB mass storage so the CircuitPython web workflow can write
# files to CIRCUITPY over Wi-Fi. Required on ESP32-S3 boards — without
# this, the web editor sees the filesystem in read-only mode.
#
# Trade-off: with USB mass storage off, plugging the board into a host
# computer no longer mounts the CIRCUITPY drive. Manage files via the
# web editor at https://code.circuitpython.org/ instead. Comment out
# the call below (and hard-reset) if you need USB drive access back.
#
# See: https://learn.adafruit.com/getting-started-with-web-workflow-using-the-code-editor
import storage

storage.disable_usb_drive()

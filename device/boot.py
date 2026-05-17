# Disable USB mass storage so the CircuitPython web workflow can write
# files to CIRCUITPY over Wi-Fi. Only applies to boards with native
# networking (e.g. MatrixPortal S3) — the MatrixPortal M4 uses an
# ESP32 co-processor over SPI and has no web workflow, so we leave the
# USB drive enabled there.
#
# Trade-off on supported boards: once USB mass storage is off, plugging
# the board into a host computer no longer mounts the CIRCUITPY drive.
# Manage files via the web editor at https://code.circuitpython.org/
# instead. Comment out the call below (via the web editor) and
# hard-reset if you need USB drive access back.
#
# See: https://learn.adafruit.com/getting-started-with-web-workflow-using-the-code-editor
import storage

try:
    import wifi  # noqa: F401 — only present on boards with native networking
except ImportError:
    pass  # no native Wi-Fi → no web workflow → keep USB drive enabled
else:
    storage.disable_usb_drive()
    # ^ comment out + hard-reset if you need the USB CIRCUITPY drive back

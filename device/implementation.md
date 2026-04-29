Claude Code: Matrix Portal M4 Implementation Guide
1. Environment Strategy
Target Path: /Volumes/CIRCUITPY/

Primary Entry Point: /Volumes/CIRCUITPY/code.py

Dependency Root: /Volumes/CIRCUITPY/lib/

2. External Library Assets
Claude should pull the latest driver bundles from CircuitPython.org. The Matrix Portal M4 requires specific drivers for the LED matrix, the M4 processor, and the ESP32 (Wi-Fi).

Main Library Bundle: CircuitPython Library Bundle (9.x)

Essential Drivers to Extract:

adafruit_matrixportal (High-level wrapper)

adafruit_portalbase (Networking/Graphics base)

adafruit_esp32spi (Wi-Fi co-processor communication)

adafruit_bitmap_font & displayio (For text/image rendering)

3. Execution Workflow
Mount Verification: Ensure the volume is mounted.

Bash
ls /Volumes/CIRCUITPY
Dependency Injection: Copy required .mpy or .py library files from the local unzipped bundle to /Volumes/CIRCUITPY/lib/.

Code Deployment: Write the logic directly to code.py.

Note: CircuitPython will auto-reload the device upon file write completion.

Log Monitoring: Use screen or tio to monitor the serial output for debugging.

Bash
# Non-interactive log tailing
cat /dev/cu.usbmodem* ```

4. macOS Management (Strict)
To prevent filesystem errors on the microcontroller, Claude must ensure hidden metadata files are purged before or during the sync process:

Cleanup Command: rm -rf /Volumes/CIRCUITPY/._* /Volumes/CIRCUITPY/.DS_Store

Recommended Sync: Use rsync -rcv --exclude='.DS_Store' [src] /Volumes/CIRCUITPY/ to ensure atomic-like updates without metadata bloat.
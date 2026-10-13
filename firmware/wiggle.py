# Turns the board into a USB mouse and glides the cursor right/left a few times.
# Run: python -m mpremote connect COM6 mount firmware exec "import wiggle"
# mpremote will error out when the board re-enumerates; the script keeps running.
# Press RESET on the board afterwards to return it to normal.
import math
import random
import time

import usb.device
from usb.device.mouse import MouseInterface

STEP_MS = 8  # matches the HID polling interval, so one small move per USB frame


def jitter(spread):
    # random float in [-spread, spread]; getrandbits is always available in MicroPython
    return (random.getrandbits(10) / 1023 * 2 - 1) * spread


def glide(m, dx, dy, ms=450):
    # Splits one move into many small steps: slow start, fast middle, slow end,
    # along a slight random arc, landing exactly on (dx, dy).
    steps = max(1, ms // STEP_MS)
    dist = math.sqrt(dx * dx + dy * dy) or 1
    px, py = -dy / dist, dx / dist  # unit vector perpendicular to the move
    arc = dist * jitter(0.08)
    sent_x = sent_y = 0
    for i in range(1, steps + 1):
        t = i / steps
        s = t * t * t * (10 - 15 * t + 6 * t * t)  # minimum-jerk easing
        bow = arc * math.sin(math.pi * t)
        x = round(dx * s + px * bow)
        y = round(dy * s + py * bow)
        m.move_by(x - sent_x, y - sent_y)
        sent_x, sent_y = x, y
        time.sleep_ms(STEP_MS)


m = MouseInterface()
# builtin_driver=True keeps the serial port alongside the new mouse
usb.device.get().init(m, builtin_driver=True)

while not m.is_open():
    time.sleep_ms(100)

time.sleep_ms(1000)

for _ in range(4):
    glide(m, 300, 0, ms=round(450 + jitter(100)))
    time.sleep_ms(round(250 + jitter(80)))
    glide(m, -300, 0, ms=round(450 + jitter(100)))
    time.sleep_ms(round(250 + jitter(80)))

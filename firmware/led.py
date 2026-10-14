# WS2812 driver for the onboard RGB LED (GPIO16), using a PIO state machine.
# MicroPython's neopixel module does not drive this LED correctly; PIO does.
import array

import rp2
from machine import Pin

LED_PIN = 16


@rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT, autopull=True, pull_thresh=24)
def _ws2812():
    T1 = 2
    T2 = 5
    T3 = 3
    wrap_target()
    label("bitloop")
    out(x, 1).side(0)[T3 - 1]
    jmp(not_x, "do_zero").side(1)[T1 - 1]
    jmp("bitloop").side(1)[T2 - 1]
    label("do_zero")
    nop().side(0)[T2 - 1]
    wrap()


class Led:
    def __init__(self, pin=LED_PIN, sm_id=0):
        self._sm = rp2.StateMachine(sm_id, _ws2812, freq=8_000_000, sideset_base=Pin(pin))
        self._sm.active(1)
        self._buf = array.array("I", [0])
        self._last = None

    def set(self, r, g, b):
        # Skips the write when the colour is unchanged, so it is cheap to call every loop.
        if (r, g, b) == self._last:
            return
        self._last = (r, g, b)
        self._buf[0] = (r << 16) | (g << 8) | b  # this board's LED takes RGB order (not the usual GRB), top 24 bits of the word
        self._sm.put(self._buf, 8)

    def off(self):
        self.set(0, 0, 0)

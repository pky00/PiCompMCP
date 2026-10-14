# Drives the onboard WS2812 (GPIO16) with a PIO state machine instead of the
# neopixel module. Cycles red, green, blue, white, then off.
# Run from RAM (nothing written to flash): python -m mpremote connect COM6 run firmware/led_test.py
import array
import time

import rp2
from machine import Pin

LED_PIN = 16


@rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT, autopull=True, pull_thresh=24)
def ws2812():
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


sm = rp2.StateMachine(0, ws2812, freq=8_000_000, sideset_base=Pin(LED_PIN))
sm.active(1)


def show(r, g, b):
    # this board's LED takes RGB order (not the usual GRB), top 24 bits of the word
    sm.put(array.array("I", [(r << 16) | (g << 8) | b]), 8)


for name, rgb in (("red", (40, 0, 0)), ("green", (0, 40, 0)), ("blue", (0, 0, 40)), ("white", (40, 40, 40))):
    print(name)
    show(*rgb)
    time.sleep(1.5)

print("off")
show(0, 0, 0)
time.sleep_ms(50)
sm.active(0)
print("done")

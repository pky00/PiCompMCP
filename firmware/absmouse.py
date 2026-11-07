# Absolute-position USB mouse: X/Y are 0..32767 across the whole screen, plus
# 5 buttons, a vertical wheel and a horizontal pan.
import struct
import time

import machine
from usb.device.hid import HIDInterface

# TinyUSB's TUD_HID_REPORT_DESC_ABSMOUSE (same layout as PiKVM / QEMU).
# Report: buttons(1) X(2) Y(2) wheel(1) pan(1) = 7 bytes, struct "<BHHbb".
# fmt: off
_DESC = (
    b'\x05\x01'          # Usage Page (Generic Desktop)
    b'\x09\x02'          # Usage (Mouse)
    b'\xA1\x01'          # Collection (Application)
    b'\x09\x01'          #   Usage (Pointer)
    b'\xA1\x00'          #   Collection (Physical)
    b'\x05\x09'          #     Usage Page (Buttons)
    b'\x19\x01'          #     Usage Minimum (1)
    b'\x29\x05'          #     Usage Maximum (5)
    b'\x15\x00'          #     Logical Minimum (0)
    b'\x25\x01'          #     Logical Maximum (1)
    b'\x95\x05'          #     Report Count (5)
    b'\x75\x01'          #     Report Size (1)
    b'\x81\x02'          #     Input (Data, Variable, Absolute): 5 button bits
    b'\x95\x01'          #     Report Count (1)
    b'\x75\x03'          #     Report Size (3)
    b'\x81\x01'          #     Input (Constant): 3 padding bits
    b'\x05\x01'          #     Usage Page (Generic Desktop)
    b'\x09\x30'          #     Usage (X)
    b'\x09\x31'          #     Usage (Y)
    b'\x15\x00'          #     Logical Minimum (0)
    b'\x26\xFF\x7F'      #     Logical Maximum (32767)
    b'\x75\x10'          #     Report Size (16)
    b'\x95\x02'          #     Report Count (2)
    b'\x81\x02'          #     Input (Data, Variable, Absolute): X, Y
    b'\x09\x38'          #     Usage (Wheel)
    b'\x15\x81'          #     Logical Minimum (-127)
    b'\x25\x7F'          #     Logical Maximum (127)
    b'\x95\x01'          #     Report Count (1)
    b'\x75\x08'          #     Report Size (8)
    b'\x81\x06'          #     Input (Data, Variable, Relative): wheel
    b'\x05\x0C'          #     Usage Page (Consumer)
    b'\x0A\x38\x02'      #     Usage (AC Pan)
    b'\x15\x81'          #     Logical Minimum (-127)
    b'\x25\x7F'          #     Logical Maximum (127)
    b'\x95\x01'          #     Report Count (1)
    b'\x75\x08'          #     Report Size (8)
    b'\x81\x06'          #     Input (Data, Variable, Relative): horizontal pan
    b'\xC0'              #   End Collection
    b'\xC0'              # End Collection
)
# fmt: on

_SEND_TIMEOUT_MS = 20


class AbsMouse(HIDInterface):
    def __init__(self):
        super().__init__(_DESC, protocol=0, interface_str="PiComp Absolute Mouse", interval_ms=1)
        self._buf = bytearray(7)

    def desc_cfg(self, desc, itf_num, ep_num, strs):
        # hid.py sets self.protocol = 1 at the end of desc_cfg, and desc_cfg runs twice
        # (sizing pass, then real pass). Reset it so the real pass doesn't advertise a
        # boot-protocol keyboard.
        self.protocol = 0
        super().desc_cfg(desc, itf_num, ep_num, strs)

    def send(self, buttons, x, y, wheel=0, pan=0):
        # Returns False if the host isn't polling (not open, suspended, re-enumerating).
        if not (x or y):
            x = 1  # Windows ignores an absolute report at exactly (0, 0); x=1 is still pixel 0
        deadline = time.ticks_add(time.ticks_ms(), _SEND_TIMEOUT_MS)
        # Don't touch the buffer while the previous report is still being sent.
        while self.busy():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            machine.idle()
        struct.pack_into("<BHHbb", self._buf, 0, buttons, x, y, wheel, pan)
        return self.send_report(self._buf, _SEND_TIMEOUT_MS)

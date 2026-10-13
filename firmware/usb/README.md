Unmodified copy of MicroPython's USB device library (MIT, Angus Gratton) from
micropython-lib commit f652fb2dfdd231e9c9c0cc1a10e12fdc9025ce95:

- `device/__init__.py`, `device/core.py` from `micropython/usb/usb-device`
- `device/hid.py` from `micropython/usb/usb-device-hid`
- `device/mouse.py` from `micropython/usb/usb-device-mouse` (used by `wiggle.py`)
- `device/keyboard.py` from `micropython/usb/usb-device-keyboard` (v0.1.1)
- `device/cdc.py` from `micropython/usb/usb-device-cdc` (v0.1.4)

Copied onto the board's filesystem with:

    python -m mpremote connect COM6 cp -r firmware/usb :

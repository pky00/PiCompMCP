"""US-layout map between HID keyboard usages and the (set-1 scan code, extended flag)
pairs that Windows low-level keyboard hooks report.

This table is the only place these values live: fwcheck keyscan (M3b) checks it
against what the hooks actually see, and corrections go here.
"""

# fmt: off
USAGE_TO_SCAN = {
    # letters a-z
    0x04: (0x1E, False), 0x05: (0x30, False), 0x06: (0x2E, False), 0x07: (0x20, False),
    0x08: (0x12, False), 0x09: (0x21, False), 0x0A: (0x22, False), 0x0B: (0x23, False),
    0x0C: (0x17, False), 0x0D: (0x24, False), 0x0E: (0x25, False), 0x0F: (0x26, False),
    0x10: (0x32, False), 0x11: (0x31, False), 0x12: (0x18, False), 0x13: (0x19, False),
    0x14: (0x10, False), 0x15: (0x13, False), 0x16: (0x1F, False), 0x17: (0x14, False),
    0x18: (0x16, False), 0x19: (0x2F, False), 0x1A: (0x11, False), 0x1B: (0x2D, False),
    0x1C: (0x15, False), 0x1D: (0x2C, False),
    # digits 1-9, 0
    0x1E: (0x02, False), 0x1F: (0x03, False), 0x20: (0x04, False), 0x21: (0x05, False),
    0x22: (0x06, False), 0x23: (0x07, False), 0x24: (0x08, False), 0x25: (0x09, False),
    0x26: (0x0A, False), 0x27: (0x0B, False),
    # Enter Esc Backspace Tab Space - = [ ] \
    0x28: (0x1C, False), 0x29: (0x01, False), 0x2A: (0x0E, False), 0x2B: (0x0F, False),
    0x2C: (0x39, False), 0x2D: (0x0C, False), 0x2E: (0x0D, False), 0x2F: (0x1A, False),
    0x30: (0x1B, False), 0x31: (0x2B, False),
    # 0x32 (non-US #) is omitted: Windows reports the same scan code as 0x31.
    # ; ' ` , . / CapsLock
    0x33: (0x27, False), 0x34: (0x28, False), 0x35: (0x29, False), 0x36: (0x33, False),
    0x37: (0x34, False), 0x38: (0x35, False), 0x39: (0x3A, False),
    # F1-F12
    0x3A: (0x3B, False), 0x3B: (0x3C, False), 0x3C: (0x3D, False), 0x3D: (0x3E, False),
    0x3E: (0x3F, False), 0x3F: (0x40, False), 0x40: (0x41, False), 0x41: (0x42, False),
    0x42: (0x43, False), 0x43: (0x44, False), 0x44: (0x57, False), 0x45: (0x58, False),
    # PrintScreen ScrollLock Pause
    0x46: (0x37, True), 0x47: (0x46, False), 0x48: (0x45, False),
    # Insert Home PageUp Delete End PageDown Right Left Down Up
    0x49: (0x52, True), 0x4A: (0x47, True), 0x4B: (0x49, True), 0x4C: (0x53, True),
    0x4D: (0x4F, True), 0x4E: (0x51, True), 0x4F: (0x4D, True), 0x50: (0x4B, True),
    0x51: (0x50, True), 0x52: (0x48, True),
    # NumLock: Windows reports it as E0 45 (and Pause as plain 45)
    0x53: (0x45, True),
    # keypad / * - + Enter 1-9 0 .
    0x54: (0x35, True), 0x55: (0x37, False), 0x56: (0x4A, False), 0x57: (0x4E, False),
    0x58: (0x1C, True),
    0x59: (0x4F, False), 0x5A: (0x50, False), 0x5B: (0x51, False), 0x5C: (0x4B, False),
    0x5D: (0x4C, False), 0x5E: (0x4D, False), 0x5F: (0x47, False), 0x60: (0x48, False),
    0x61: (0x49, False), 0x62: (0x52, False), 0x63: (0x53, False),
    # non-US \ and Application (menu)
    0x64: (0x56, False), 0x65: (0x5D, True),
    # modifiers: LCtrl LShift LAlt LGui RCtrl RShift RAlt RGui. The layout marks Right
    # Shift extended ("T36 | KBDEXT" in kbdus.c), so every keyboard's reports as E0 36.
    0xE0: (0x1D, False), 0xE1: (0x2A, False), 0xE2: (0x38, False), 0xE3: (0x5B, True),
    0xE4: (0x1D, True), 0xE5: (0x36, True), 0xE6: (0x38, True), 0xE7: (0x5C, True),
}
# fmt: on

SCAN_TO_USAGE = {v: k for k, v in USAGE_TO_SCAN.items()}

# While Alt is down Windows reports PrintScreen as SysRq (54), and while Ctrl is down
# Pause as Break (E0 46). On replay the board's own Alt or Ctrl does the same.
_ALIASES = {(0x54, False): 0x46, (0x46, True): 0x48}

# kbd.h SCANCODE_SIMULATED: on keystrokes Windows makes up itself. With NumLock on it
# lifts Shift around a keypad key (scan 0x236 / 0x22A) so that Shift+keypad 4 acts as Left.
SCANCODE_SIMULATED = 0x200

# Right Shift and NumLock arrive without the E0 prefix and get their extended flag
# from the layout. SendInput applies the layout again, so it must get them plain:
# E0 36 and E0 45 map to no key.
_SENT_PLAIN = frozenset({(0x36, True), (0x45, True)})

VK_RSHIFT = 0xA1

ESC = 41
KEY_R = 21
LCTRL, LSHIFT, LALT, LGUI, RCTRL, RSHIFT, RALT, RGUI = range(224, 232)
CTRL_USAGES = (LCTRL, RCTRL)
ALT_USAGES = (LALT, RALT)
CTRL_ALT_SCANS = frozenset({(0x1D, False), (0x1D, True), (0x38, False), (0x38, True)})


def usage_for(scan, ext):
    # None for unmapped keys (media, volume, Fn layer). A fake E0 36 shares Right
    # Shift's entry, so callers check is_fake_shift first.
    key = (scan, bool(ext))
    return SCAN_TO_USAGE.get(key, _ALIASES.get(key))


def scan_for(usage):
    return USAGE_TO_SCAN.get(usage)


def send_scan_for(usage):
    # The (scan code, extended flag) to give SendInput for usage.
    sk = USAGE_TO_SCAN.get(usage)
    if sk in _SENT_PLAIN:
        return sk[0], False
    return sk


def is_fake_shift(scan, ext, vk):
    # Shift presses nobody made:
    # - Windows' own, around a keypad key (SCANCODE_SIMULATED). The Shift it presses
    #   again afterwards carries the plain scan code and reads as auto-repeat.
    # - a PS/2 keyboard's E0 2A / E0 36 around the gray navigation keys (NumLock on,
    #   or Shift held). The layout maps neither to a key, so the hook's vk tells a
    #   fake E0 36 from Right Shift.
    if scan & SCANCODE_SIMULATED:
        return True
    if not ext:
        return False
    return scan == 0x2A or (scan == 0x36 and vk != VK_RSHIFT)

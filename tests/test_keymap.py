# picomp/keymap.py: the HID usage <-> (scan code, extended) table must be one-to-one,
# because recording maps scan -> usage and the replay's echo check maps it back.
import pytest

from picomp import keymap

EXPECTED_USAGES = (set(range(4, 102)) - {0x32}) | set(range(224, 232))


def test_usage_set_is_exact():
    assert set(keymap.USAGE_TO_SCAN) == EXPECTED_USAGES
    assert len(keymap.USAGE_TO_SCAN) == 105


def test_table_is_one_to_one():
    scans = list(keymap.USAGE_TO_SCAN.values())
    assert len(set(scans)) == len(scans)
    assert keymap.SCAN_TO_USAGE == {s: u for u, s in keymap.USAGE_TO_SCAN.items()}


def test_entries_are_scan_ext_pairs():
    for usage, (scan, ext) in keymap.USAGE_TO_SCAN.items():
        assert isinstance(scan, int) and 0x01 <= scan <= 0x7F, hex(usage)
        assert type(ext) is bool, hex(usage)


def test_round_trip():
    for usage in EXPECTED_USAGES:
        scan, ext = keymap.scan_for(usage)
        assert keymap.usage_for(scan, ext) == usage, hex(usage)


def test_usages_outside_the_table():
    # 0x32 shares its scan code with 0x31; 0x68 (F13) and up cannot be sent.
    for usage in (0, 1, 2, 3, 0x32, 0x66, 0x68, 0xDF, 0xE8, 300):
        assert keymap.scan_for(usage) is None, hex(usage)


@pytest.mark.parametrize("vk", [0x00, 0xA0, 0xFF])
def test_e0_2a_is_always_a_fake_shift(vk):
    assert keymap.is_fake_shift(0x2A, True, vk)
    assert keymap.usage_for(0x2A, True) is None


@pytest.mark.parametrize("vk", [0x00, 0xFF])
def test_e0_36_without_vk_rshift_is_a_fake_shift(vk):
    # The layout maps E0 36 to no key, so a PS/2 keyboard's fake never has VK_RSHIFT.
    assert keymap.is_fake_shift(0x36, True, vk)


@pytest.mark.parametrize("scan, ext, vk", [(0x236, True, 0xA1), (0x22A, False, 0xA0)])
def test_shifts_windows_simulates_are_fake(scan, ext, vk):
    # Hardware log, NumLock on: Shift+keypad 4 puts these around the keypad key.
    assert keymap.is_fake_shift(scan, ext, vk)


def test_sysrq_and_break_record_as_printscreen_and_pause():
    # Hardware log: Alt+PrintScreen gives 54 vk 2C, Ctrl+Pause gives E0 46 vk 03.
    assert keymap.usage_for(0x54, False) == 0x46
    assert keymap.usage_for(0x46, True) == 0x48
    assert keymap.scan_for(0x46) == (0x37, True)  # the board still sends plain PrintScreen
    assert keymap.scan_for(0x48) == (0x45, False)


def test_right_shift_is_extended():
    # M3b keyscan: the board's Right Shift reports as E0 36 vk A1, like every keyboard's.
    assert keymap.scan_for(keymap.RSHIFT) == (0x36, True)
    assert keymap.usage_for(0x36, True) == keymap.RSHIFT
    assert not keymap.is_fake_shift(0x36, True, keymap.VK_RSHIFT)
    assert keymap.usage_for(0x36, False) is None


def test_real_shifts_are_mapped():
    assert keymap.usage_for(0x2A, False) == keymap.LSHIFT
    assert keymap.usage_for(0x36, True) == keymap.RSHIFT
    assert not keymap.is_fake_shift(0x2A, False, 0xA0)
    assert not keymap.is_fake_shift(0x36, True, 0xA1)
    assert not keymap.is_fake_shift(0x36, False, 0xA1)


def test_send_scan_drops_the_flag_the_layout_adds():
    assert keymap.send_scan_for(keymap.RSHIFT) == (0x36, False)
    assert keymap.send_scan_for(0x53) == (0x45, False)  # NumLock
    for usage in EXPECTED_USAGES - {keymap.RSHIFT, 0x53}:
        assert keymap.send_scan_for(usage) == keymap.scan_for(usage), hex(usage)
    assert keymap.send_scan_for(0x32) is None


def test_unknown_scan_codes_are_unmapped():
    # mute, volume down, volume up, play/pause
    for scan, ext in ((0x20, True), (0x2E, True), (0x30, True), (0x22, True), (0x7F, False), (0x00, False)):
        assert keymap.usage_for(scan, ext) is None


def test_ext_flag_is_normalised():
    assert keymap.usage_for(0x1D, 1) == keymap.RCTRL
    assert keymap.usage_for(0x1D, 0) == keymap.LCTRL
    assert keymap.is_fake_shift(0x2A, 1, 0)
    assert not keymap.is_fake_shift(0x2A, 0, 0xA0)


def test_modifier_constants():
    names = (keymap.LCTRL, keymap.LSHIFT, keymap.LALT, keymap.LGUI,
             keymap.RCTRL, keymap.RSHIFT, keymap.RALT, keymap.RGUI)
    assert names == tuple(range(224, 232))
    assert keymap.CTRL_USAGES == (224, 228)
    assert keymap.ALT_USAGES == (226, 230)
    assert keymap.CTRL_ALT_SCANS == {keymap.scan_for(u) for u in keymap.CTRL_USAGES + keymap.ALT_USAGES}


def test_hotkey_usages():
    assert (keymap.ESC, keymap.KEY_R) == (41, 21)


@pytest.mark.parametrize("usage, scan", [
    (keymap.ESC, (0x01, False)),
    (keymap.KEY_R, (0x13, False)),
    (0x04, (0x1E, False)),
    (0x28, (0x1C, False)),
    (keymap.LCTRL, (0x1D, False)),
    (keymap.RCTRL, (0x1D, True)),
    (keymap.LALT, (0x38, False)),
    (keymap.RALT, (0x38, True)),
])
def test_keys_the_hotkeys_and_touch_check_depend_on(usage, scan):
    assert keymap.scan_for(usage) == scan

"""Hook filters and the held-key model, driven with SimpleNamespace hook structs,
a stubbed GetAsyncKeyState, button swap and suppress: no pynput listener runs
and no Win32 function is called."""
import sys
from types import SimpleNamespace

import pytest

from picomp import hooks
from picomp.hooks import (VK_CONTROL, VK_MENU, WM_KEYDOWN, WM_KEYUP, WM_MOUSEMOVE, WM_SYSKEYDOWN,
                          WM_SYSKEYUP, Ev, HeldModel, Hooks, button_ev, hotkey_ev, key_ev, move_ev,
                          wheel_ev)

MS = 1_000_000


def kbd_data(vk, scan, flags=0):
    return SimpleNamespace(vkCode=vk, scanCode=scan, flags=flags)


def mouse_data(x=0, y=0, data=0, flags=0):
    return SimpleNamespace(pt=SimpleNamespace(x=x, y=y), mouseData=data, flags=flags)


R = kbd_data(hooks.VK_R, 0x13)
ESC = kbd_data(hooks.VK_ESCAPE, 0x01)
A = kbd_data(0x41, 0x1E)
LCTRL = kbd_data(0xA2, 0x1D)
LALT = kbd_data(0xA4, 0x38)

R_DOWN = key_ev(0, 0x13, False, True, vk=0x52)
R_UP = key_ev(0, 0x13, False, False, vk=0x52)
ESC_DOWN = key_ev(0, 0x01, False, True, vk=0x1B)
ESC_UP = key_ev(0, 0x01, False, False, vk=0x1B)


class AsyncKeys:
    # GetAsyncKeyState stub: the virtual keys Windows reports as down.
    def __init__(self, *down):
        self.down = set(down)

    def __call__(self, vk):
        return vk in self.down


class Rig:
    def __init__(self, *down, swapped=False):
        self.keys = AsyncKeys(*down)
        self.events = []
        self.suppressed = 0
        self.swapped = swapped  # GetSystemMetrics(SM_SWAPBUTTON) stub, read at start()
        self.hooks = Hooks(self.events.append, key_down=self.keys, suppress=self.suppress,
                           buttons_swapped=lambda: self.swapped)

    def suppress(self):
        self.suppressed += 1

    def fake_listeners(self):
        made = []

        def new_listeners():
            pair = (FakeListener(), FakeListener())
            made.append(pair)
            return pair

        self.hooks._new_listeners = new_listeners
        return made

    def key(self, msg, data):
        assert self.hooks.kbd_filter(msg, data) is False

    def mouse(self, msg, data):
        assert self.hooks.mouse_filter(msg, data) is False

    def evs(self):
        # Hook timestamps are real perf_counter_ns values.
        return [e._replace(t_ns=0) for e in self.events]


class FakeListener:
    name = "fake"

    def __init__(self):
        self.calls = []
        self.alive = False

    def start(self):
        self.calls.append("start")
        self.alive = True

    def wait(self):
        self.calls.append("wait")

    def stop(self):
        self.calls.append("stop")
        self.alive = False

    def join(self, timeout=None):
        self.calls.append("join")

    def is_alive(self):
        return self.alive


def test_ctrl_alt_r_is_one_suppressed_record_hotkey():
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_KEYDOWN, R)
    assert rig.evs() == [hotkey_ev(0, "record")]
    assert isinstance(rig.events[0].t_ns, int)
    assert rig.suppressed == 1


def test_ctrl_alt_esc_is_the_kill_hotkey():
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_SYSKEYDOWN, ESC)
    rig.key(WM_SYSKEYUP, ESC)
    assert rig.evs() == [hotkey_ev(0, "kill")]
    assert rig.suppressed == 2


def test_auto_repeat_and_up_of_the_hotkey_never_toggle_again():
    rig = Rig(VK_CONTROL, VK_MENU)
    for _ in range(5):  # the press plus four auto-repeats
        rig.key(WM_KEYDOWN, R)
    rig.key(WM_KEYUP, R)
    assert rig.evs() == [hotkey_ev(0, "record")]
    assert rig.suppressed == 6
    rig.key(WM_KEYDOWN, R)  # a new press of the chord toggles again
    assert rig.evs() == [hotkey_ev(0, "record")] * 2
    assert rig.suppressed == 7


def test_flagged_key_stays_suppressed_after_ctrl_and_alt_are_released():
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_KEYDOWN, R)
    rig.keys.down.clear()
    rig.key(WM_KEYDOWN, R)
    rig.key(WM_KEYUP, R)
    assert rig.evs() == [hotkey_ev(0, "record")]
    assert rig.suppressed == 3
    rig.key(WM_KEYDOWN, R)  # the up cleared the flag
    rig.key(WM_KEYUP, R)
    assert rig.evs()[1:] == [R_DOWN, R_UP]
    assert rig.suppressed == 3


@pytest.mark.parametrize("held", [(), (VK_CONTROL,), (VK_MENU,)], ids=["none", "ctrl", "alt"])
def test_r_and_esc_without_both_ctrl_and_alt_pass_through(held):
    rig = Rig(*held)
    for data in (R, ESC):
        rig.key(WM_KEYDOWN, data)
        rig.key(WM_KEYUP, data)
    assert rig.evs() == [R_DOWN, R_UP, ESC_DOWN, ESC_UP]
    assert rig.suppressed == 0


def test_other_keys_pass_through_while_ctrl_and_alt_are_held():
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_KEYDOWN, A)
    rig.key(WM_KEYUP, A)
    assert rig.evs() == [key_ev(0, 0x1E, False, True, vk=0x41), key_ev(0, 0x1E, False, False, vk=0x41)]
    assert rig.suppressed == 0


@pytest.mark.parametrize("flag", [hooks.LLKHF_INJECTED, hooks.LLKHF_LOWER_IL_INJECTED])
def test_injected_r_never_makes_a_hotkey(flag):
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_KEYDOWN, kbd_data(hooks.VK_R, 0x13, flag))
    rig.key(WM_KEYUP, kbd_data(hooks.VK_R, 0x13, flag))
    assert rig.evs() == [R_DOWN._replace(injected=True), R_UP._replace(injected=True)]
    assert rig.suppressed == 0
    rig.key(WM_KEYDOWN, R)  # the injected one left no flag behind
    assert rig.evs()[2:] == [hotkey_ev(0, "record")]


def test_stuck_modifiers_in_the_hook_stream_do_not_make_a_hotkey():
    # Ctrl+Alt+Del: the hooks saw Ctrl and Alt go down but never their ups.
    # GetAsyncKeyState already reports them up, so the later R is a plain key.
    rig = Rig()
    rig.key(WM_KEYDOWN, LCTRL)
    rig.key(WM_SYSKEYDOWN, LALT)
    rig.key(WM_SYSKEYDOWN, R)
    assert [e.kind for e in rig.events] == ["key", "key", "key"]
    assert rig.evs()[2] == R_DOWN
    assert rig.suppressed == 0


def test_ctrl_and_alt_the_hooks_never_saw_still_count():
    # Pressed while an elevated window had focus: only GetAsyncKeyState knows.
    rig = Rig(VK_CONTROL, VK_MENU)
    rig.key(WM_SYSKEYDOWN, R)
    assert rig.evs() == [hotkey_ev(0, "record")]


def test_key_event_fields():
    rig = Rig()
    rctrl = kbd_data(0xA3, 0x1D, hooks.LLKHF_EXTENDED)
    rig.key(WM_KEYDOWN, rctrl)
    rig.key(WM_SYSKEYUP, rctrl)
    assert rig.evs() == [key_ev(0, 0x1D, True, True, vk=0xA3), key_ev(0, 0x1D, True, False, vk=0xA3)]


def test_flagged_key_repeats_are_measured_from_the_last_one():
    # Slowest Windows settings: 1 s before the first repeat, then about one per 400 ms.
    rig = Rig(VK_CONTROL, VK_MENU)
    assert rig.hooks._kbd_event(0, WM_KEYDOWN, R) is True
    rig.keys.down.clear()  # Ctrl and Alt make no difference once R is flagged
    for t_ms in (1000, 1400, 1800, 2200):
        assert rig.hooks._kbd_event(t_ms * MS, WM_KEYDOWN, R) is True
    assert rig.hooks._kbd_event(2300 * MS, WM_KEYUP, R) is True
    assert rig.events == [hotkey_ev(0, "record")]


@pytest.mark.parametrize("held, expected", [
    ((), [R_DOWN._replace(t_ns=2000 * MS), R_UP._replace(t_ns=2100 * MS)]),
    ((VK_CONTROL, VK_MENU), [hotkey_ev(2000 * MS, "record")]),
], ids=["plain-r", "ctrl-alt-r-again"])
def test_flagged_key_whose_up_was_never_seen_is_a_new_press_after_the_repeat_gap(held, expected):
    # The R's up went to the lock screen or a UAC prompt, so the hook never saw it.
    rig = Rig(VK_CONTROL, VK_MENU)
    assert rig.hooks._kbd_event(0, WM_KEYDOWN, R) is True
    rig.keys.down = set(held)
    is_hotkey = bool(held)
    assert rig.hooks._kbd_event(2000 * MS, WM_KEYDOWN, R) is is_hotkey
    assert rig.hooks._kbd_event(2100 * MS, WM_KEYUP, R) is is_hotkey
    assert rig.events == [hotkey_ev(0, "record")] + expected


def test_restart_recreates_the_listeners_and_clears_flagged_keys():
    rig = Rig(VK_CONTROL, VK_MENU)
    made = rig.fake_listeners()
    rig.hooks.start()
    rig.hooks.start()
    assert len(made) == 1
    rig.key(WM_KEYDOWN, R)
    rig.hooks.restart()
    # The R's up went to the old hook, so its flag must not swallow the next press.
    rig.key(WM_KEYDOWN, R)
    assert rig.evs() == [hotkey_ev(0, "record")] * 2
    assert len(made) == 2
    for listener in made[0]:
        assert listener.calls == ["start", "wait", "stop", "join"]
    for listener in made[1]:
        assert listener.calls == ["start", "wait"]
    rig.hooks.stop()
    rig.hooks.stop()
    for listener in made[1]:
        assert listener.calls == ["start", "wait", "stop", "join"]


def test_constructing_hooks_does_not_import_pynput():
    before = "pynput" in sys.modules
    Hooks(lambda ev: None)
    assert ("pynput" in sys.modules) == before


def test_filters_never_raise():
    def broken(ev):
        raise RuntimeError("queue gone")

    calls = []
    h = Hooks(broken, key_down=AsyncKeys(), suppress=lambda: calls.append(1))
    assert h.kbd_filter(WM_KEYDOWN, A) is False
    assert h.kbd_filter(WM_KEYDOWN, object()) is False
    assert h.mouse_filter(WM_MOUSEMOVE, mouse_data(1, 2)) is False
    assert h.mouse_filter(WM_MOUSEMOVE, object()) is False
    assert calls == []


def test_mouse_move_keeps_the_raw_point():
    rig = Rig()
    rig.mouse(WM_MOUSEMOVE, mouse_data(-5, 2000))
    assert rig.evs() == [move_ev(0, -5, 2000)]


@pytest.mark.parametrize("msg, data, button, down", [
    (0x201, 0, 1, True), (0x202, 0, 1, False),
    (0x204, 0, 2, True), (0x205, 0, 2, False),
    (0x207, 0, 3, True), (0x208, 0, 3, False),
    (0x20B, 1 << 16, 4, True), (0x20C, 1 << 16, 4, False),
    (0x20B, 2 << 16, 5, True), (0x20C, 2 << 16, 5, False),
])
def test_mouse_buttons(msg, data, button, down):
    rig = Rig()
    rig.mouse(msg, mouse_data(10, 20, data))
    assert rig.evs() == [button_ev(0, button, down, 10, 20)]


@pytest.mark.parametrize("msg, data, button", [
    (0x201, 0, 2), (0x202, 0, 2),  # WM_LBUTTON*: the logical primary, the physical right
    (0x204, 0, 1), (0x205, 0, 1),
    (0x207, 0, 3), (0x20B, 1 << 16, 4), (0x20C, 2 << 16, 5),
])
def test_swapped_buttons_come_out_as_the_physical_button(msg, data, button):
    ev = hooks._mouse_ev(0, msg, mouse_data(10, 20, data), swapped=True)
    assert ev == button_ev(0, button, msg in (0x201, 0x204, 0x207, 0x20B), 10, 20)


def test_start_and_restart_read_the_button_swap():
    rig = Rig(swapped=True)
    rig.fake_listeners()
    rig.mouse(0x201, mouse_data(1, 1))  # not started yet: nothing read, no swap
    rig.hooks.start()
    rig.mouse(0x201, mouse_data(1, 1))
    rig.swapped = False  # changed in Settings while running: seen at the next restart
    rig.mouse(0x201, mouse_data(1, 1))
    rig.hooks.restart()
    rig.mouse(0x201, mouse_data(1, 1))
    assert [e.button for e in rig.events] == [1, 2, 2, 1]


@pytest.mark.parametrize("data", [0, 3 << 16])
def test_unknown_x_button_is_ignored(data):
    rig = Rig()
    rig.mouse(0x20B, mouse_data(1, 1, data))
    assert rig.events == []


@pytest.mark.parametrize("msg, data, dv, dh", [
    (0x20A, 120 << 16, 120, 0),
    (0x20A, 0xFF88 << 16, -120, 0),  # the high word of the DWORD is a signed short
    (0x20A, (0xFF10 << 16) | 0x1234, -240, 0),  # the low word is not part of the delta
    (0x20A, -120 << 16, -120, 0),
    (0x20E, 120 << 16, 0, 120),
    (0x20E, 0xFF88 << 16, 0, -120),
])
def test_wheel_delta_and_sign(msg, data, dv, dh):
    rig = Rig()
    rig.mouse(msg, mouse_data(7, 8, data))
    assert rig.evs() == [wheel_ev(0, dv, dh, 7, 8)]


@pytest.mark.parametrize("flags, injected", [
    (0, False), (hooks.LLMHF_INJECTED, True), (hooks.LLMHF_LOWER_IL_INJECTED, True)])
def test_mouse_injected_flags(flags, injected):
    rig = Rig()
    rig.mouse(WM_MOUSEMOVE, mouse_data(1, 2, flags=flags))
    rig.mouse(0x201, mouse_data(1, 2, flags=flags))
    rig.mouse(0x20A, mouse_data(1, 2, 120 << 16, flags))
    assert [e.injected for e in rig.events] == [injected] * 3


@pytest.mark.parametrize("msg", [0x000, 0x203, 0x209, 0x2A1])
def test_other_mouse_messages_are_ignored(msg):
    rig = Rig()
    rig.mouse(msg, mouse_data(1, 1, 120 << 16))
    assert rig.events == []


def test_mouse_filter_never_suppresses():
    rig = Rig(VK_CONTROL, VK_MENU)
    for msg in (0x200, 0x201, 0x202, 0x20A, 0x20E):
        rig.mouse(msg, mouse_data(1, 1, 120 << 16))
    assert len(rig.events) == 5
    assert rig.suppressed == 0


def test_event_constructors_fill_only_their_fields():
    assert key_ev(5, 0x1D, 1, True, vk=0xA3) == Ev(5, "key", True, 0, 0, 0, 0, 0, 0xA3, 0x1D, True, False, None)
    assert wheel_ev(5, 0, -120, 3, 4) == Ev(5, "wheel", False, 3, 4, 0, 0, -120, 0, 0, False, False, None)
    assert button_ev(5, 4, True, 3, 4, injected=1) == Ev(5, "button", True, 3, 4, 4, injected=True)
    assert hotkey_ev(5, "shutdown") == Ev(5, "hotkey", hotkey="shutdown")


def test_held_model_tracks_keys_and_buttons():
    held = HeldModel()
    held.update(key_ev(0, 0x1E, False, True, vk=0x41))
    held.update(key_ev(0, 0x1D, True, True, vk=0xA3))
    held.update(key_ev(0, 0x30, True, True, vk=0xAF))  # volume up: unmapped, still held
    held.update(button_ev(0, 1, True, 5, 5))
    assert held.snapshot() == ({(0x1E, False): 0x41, (0x1D, True): 0xA3, (0x30, True): 0xAF}, {1})
    held.update(key_ev(0, 0x1E, False, False, vk=0x41))
    held.update(key_ev(0, 0x30, True, False, vk=0xAF))
    held.update(button_ev(0, 1, False, 5, 5))
    assert held.snapshot() == ({(0x1D, True): 0xA3}, set())


def test_held_model_ignores_injected_hotkeys_fake_shifts_moves_and_wheels():
    held = HeldModel()
    for ev in (key_ev(0, 0x1E, False, True, vk=0x41, injected=True),
               button_ev(0, 1, True, 5, 5, injected=True),
               hotkey_ev(0, "record"),
               key_ev(0, 0x2A, True, True, vk=0xA0),
               key_ev(0, 0x36, True, True, vk=0xFF),
               key_ev(0, 0x236, True, True, vk=0xA1),  # Windows' own, around a keypad key
               move_ev(0, 5, 5),
               wheel_ev(0, 120, 0, 5, 5)):
        held.update(ev)
    assert held.snapshot() == ({}, set())
    held.update(key_ev(0, 0x1E, False, True, vk=0x41))
    held.update(key_ev(0, 0x1E, False, False, vk=0x41, injected=True))
    assert held.snapshot() == ({(0x1E, False): 0x41}, set())


def test_held_model_tracks_right_shift():
    # Right Shift is E0 36 too; only its vk tells it from a fake shift.
    held = HeldModel()
    held.update(key_ev(0, 0x36, True, True, vk=0xA1))
    assert held.snapshot() == ({(0x36, True): 0xA1}, set())
    held.update(key_ev(0, 0x36, True, False, vk=0xA1))
    assert held.snapshot() == ({}, set())


def test_held_model_snapshot_is_a_copy():
    held = HeldModel()
    held.update(key_ev(0, 0x1E, False, True, vk=0x41))
    keys, buttons = held.snapshot()
    keys.clear()
    buttons.add(3)
    assert held.snapshot() == ({(0x1E, False): 0x41}, set())


def test_reconcile_drops_items_windows_reports_up():
    held = HeldModel()
    held.update(key_ev(0, 0x1E, False, True, vk=0x41))
    held.update(key_ev(0, 0x1D, False, True, vk=0xA2))  # its up went to the lock screen
    held.update(button_ev(0, 1, True, 0, 0))
    held.update(button_ev(200 * MS, 2, True, 0, 0))
    down = AsyncKeys(0x41, 0x101)

    def button_vk(code):
        return 0x100 + code

    held.reconcile(300 * MS, key_down=down, button_vk=button_vk)
    assert held.snapshot() == ({(0x1E, False): 0x41}, {1, 2})  # button 2 is too young to check
    held.reconcile(500 * MS, key_down=down, button_vk=button_vk)
    assert held.snapshot() == ({(0x1E, False): 0x41}, {1})


def test_reconcile_measures_from_the_first_down_not_the_repeats():
    held = HeldModel()
    held.update(key_ev(0, 0x1E, False, True, vk=0x41))
    held.update(key_ev(200 * MS, 0x1E, False, True, vk=0x41))
    held.reconcile(300 * MS, key_down=AsyncKeys(), button_vk=lambda code: code)
    assert held.snapshot() == ({}, set())

"""Windows low-level keyboard and mouse hooks (two pynput listeners).

The filters only timestamp and enqueue raw events and return False, so pynput's
own callbacks never run. Nothing is suppressed except the R of Ctrl+Alt+R and
the Esc of Ctrl+Alt+Esc (plus their auto-repeats and key-ups).
"""
import collections
import ctypes
import logging
import threading
import time

from picomp import keymap, win

log = logging.getLogger(__name__)

WM_KEYDOWN = 0x100
WM_KEYUP = 0x101
WM_SYSKEYDOWN = 0x104
WM_SYSKEYUP = 0x105
WM_MOUSEMOVE = 0x200
WM_LBUTTONDOWN = 0x201
WM_LBUTTONUP = 0x202
WM_RBUTTONDOWN = 0x204
WM_RBUTTONUP = 0x205
WM_MBUTTONDOWN = 0x207
WM_MBUTTONUP = 0x208
WM_MOUSEWHEEL = 0x20A
WM_XBUTTONDOWN = 0x20B
WM_XBUTTONUP = 0x20C
WM_MOUSEHWHEEL = 0x20E

VK_ESCAPE = 0x1B
VK_R = 0x52
VK_CONTROL = 0x11
VK_MENU = 0x12

LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

_KEY_DOWN_MSGS = (WM_KEYDOWN, WM_SYSKEYDOWN)
_KEY_INJECTED = LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
_MOUSE_INJECTED = LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED

# message -> (button code, down)
_BUTTON_MSGS = {
    WM_LBUTTONDOWN: (1, True),
    WM_LBUTTONUP: (1, False),
    WM_RBUTTONDOWN: (2, True),
    WM_RBUTTONUP: (2, False),
    WM_MBUTTONDOWN: (3, True),
    WM_MBUTTONUP: (3, False),
}
_XBUTTON_CODES = {1: 4, 2: 5}  # XBUTTON1 = back, XBUTTON2 = forward

_HELD_CHECK_NS = 250_000_000  # only items held this long are checked against GetAsyncKeyState
# Longer than Windows' longest repeat delay (1 s) and slowest repeat period (about
# 400 ms): a flagged key silent for this long lost its up to the secure desktop
# or an elevated window, so its next down is a new press.
_REPEAT_GAP_NS = 1_500_000_000

Ev = collections.namedtuple(
    "Ev", "t_ns kind down x y button dv dh vk scan ext injected hotkey",
    defaults=(False, 0, 0, 0, 0, 0, 0, 0, False, False, None))


def move_ev(t_ns, x, y, injected=False):
    return Ev(t_ns, "move", x=x, y=y, injected=bool(injected))


def button_ev(t_ns, button, down, x, y, injected=False):
    return Ev(t_ns, "button", down=bool(down), x=x, y=y, button=button, injected=bool(injected))


def wheel_ev(t_ns, dv, dh, x, y, injected=False):
    return Ev(t_ns, "wheel", x=x, y=y, dv=dv, dh=dh, injected=bool(injected))


def key_ev(t_ns, scan, ext, down, vk=0, injected=False):
    return Ev(t_ns, "key", down=bool(down), vk=vk, scan=scan, ext=bool(ext), injected=bool(injected))


def hotkey_ev(t_ns, name):
    return Ev(t_ns, "hotkey", hotkey=name)


def _high_word(mouse_data):
    return (mouse_data >> 16) & 0xFFFF


def _mouse_ev(t, msg, data, swapped=False):
    x, y = data.pt.x, data.pt.y
    injected = bool(data.flags & _MOUSE_INJECTED)
    if msg == WM_MOUSEMOVE:
        return move_ev(t, x, y, injected)
    if msg in _BUTTON_MSGS:
        code, down = _BUTTON_MSGS[msg]
        if swapped and code in (1, 2):
            # The hook names the logical button; the board, SendInput and
            # GetAsyncKeyState name physical ones, and Windows swaps those again.
            code = 3 - code
        return button_ev(t, code, down, x, y, injected)
    if msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        code = _XBUTTON_CODES.get(_high_word(data.mouseData))
        if code is None:
            return None
        return button_ev(t, code, msg == WM_XBUTTONDOWN, x, y, injected)
    if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        delta = ctypes.c_short(_high_word(data.mouseData)).value
        if msg == WM_MOUSEWHEEL:
            return wheel_ev(t, delta, 0, x, y, injected)
        return wheel_ev(t, 0, delta, x, y, injected)
    return None


class Hooks:
    def __init__(self, on_event, key_down=None, suppress=None, buttons_swapped=None):
        self._on_event = on_event
        self._key_down = key_down or win.key_down
        self._suppress = suppress or self._suppress_current
        self._buttons_swapped = buttons_swapped or win.buttons_swapped
        self._swapped = False  # read at start(), so restart() picks up a changed setting
        self._flagged = {}  # vkCode of a hotkey key still down -> t_ns of its last event
        self._kbd = None
        self._mouse = None
        self._listeners = []
        self._lock = threading.RLock()  # start/stop/restart can come from different HTTP threads

    def _suppress_current(self):
        # pynput's suppress_event just raises its SuppressException, which the
        # hook turns into "swallow this event"; any listener instance will do.
        self._kbd.suppress_event()

    def _new_listeners(self):
        from pynput import keyboard, mouse
        kbd = keyboard.Listener(win32_event_filter=self.kbd_filter)
        ms = mouse.Listener(win32_event_filter=self.mouse_filter)
        return kbd, ms

    def start(self):
        with self._lock:
            if self._listeners:
                return
            self._swapped = bool(self._buttons_swapped())
            kbd, ms = self._new_listeners()
            self._kbd, self._mouse = kbd, ms
            started = []
            try:
                for listener in (kbd, ms):
                    listener.start()
                    started.append(listener)
                for listener in started:
                    listener.wait()
            except Exception:
                self._stop_all(started)
                raise
            self._listeners = started
            log.info("input hooks started")

    def stop(self):
        with self._lock:
            listeners, self._listeners = self._listeners, []
            if listeners:
                self._stop_all(listeners)
                log.info("input hooks stopped")

    def restart(self):
        # Stop before start: a short gap is harmless, duplicate events are not.
        with self._lock:
            self.stop()
            self._flagged.clear()
            self.start()

    def _stop_all(self, listeners):
        for listener in listeners:
            try:
                listener.stop()
            except Exception:
                log.exception("could not stop an input listener")
        for listener in listeners:
            try:
                listener.join(1)  # pynput's join re-raises an error from the listener thread
            except Exception:
                log.exception("input listener ended with an error")
            if listener.is_alive():
                log.warning("input listener %s did not stop within 1 s", listener.name)

    def kbd_filter(self, msg, data):
        t = time.perf_counter_ns()
        try:
            suppress = self._kbd_event(t, msg, data)
        except Exception:
            log.exception("keyboard hook filter failed")
            suppress = False
        if suppress:
            self._suppress()  # outside the try: pynput's SuppressException must reach the hook
        return False

    def _kbd_event(self, t, msg, data):
        # Returns True when the event must be suppressed.
        vk, scan, flags = data.vkCode, data.scanCode, data.flags
        down = msg in _KEY_DOWN_MSGS
        injected = bool(flags & _KEY_INJECTED)
        last = self._flagged.get(vk)
        if last is not None:
            if not down:
                self._flagged.pop(vk, None)
                return True
            if t - last <= _REPEAT_GAP_NS:
                self._flagged[vk] = t  # auto-repeat
                return True
            self._flagged.pop(vk, None)
        # GetAsyncKeyState covers both Ctrl and both Alt keys and already reflects
        # ups the hooks missed (Ctrl+Alt+Del, Win+L).
        if (down and not injected and vk in (VK_ESCAPE, VK_R)
                and self._key_down(VK_CONTROL) and self._key_down(VK_MENU)):
            self._flagged[vk] = t
            self._on_event(hotkey_ev(t, "kill" if vk == VK_ESCAPE else "record"))
            return True
        self._on_event(key_ev(t, scan, flags & LLKHF_EXTENDED, down, vk, injected))
        return False

    def mouse_filter(self, msg, data):
        t = time.perf_counter_ns()
        try:
            ev = _mouse_ev(t, msg, data, self._swapped)
            if ev is not None:
                self._on_event(ev)
        except Exception:
            log.exception("mouse hook filter failed")
        return False


class HeldModel:
    # What Windows says is physically held right now, for touch detection.
    # Updated by the dispatcher; snapshot() may be called from any thread.

    def __init__(self):
        self._lock = threading.Lock()
        self._keys = {}  # (scan, ext) -> (vk, t_ns of the first down)
        self._buttons = {}  # code -> t_ns of the down

    def update(self, ev):
        if ev.injected or ev.kind not in ("key", "button"):
            return
        with self._lock:
            if ev.kind == "key":
                key = (ev.scan, ev.ext)
                if keymap.is_fake_shift(ev.scan, ev.ext, ev.vk):
                    return
                if ev.down:
                    self._keys.setdefault(key, (ev.vk, ev.t_ns))
                else:
                    self._keys.pop(key, None)
            elif ev.down:
                self._buttons.setdefault(ev.button, ev.t_ns)
            else:
                self._buttons.pop(ev.button, None)

    def snapshot(self):
        with self._lock:
            return {k: vk for k, (vk, _) in self._keys.items()}, set(self._buttons)

    def reconcile(self, now_ns, key_down=None, button_vk=None):
        # Drops items whose up the hooks never saw (UAC prompt, Win+L, admin window).
        key_down = key_down or win.key_down
        button_vk = button_vk or win.button_vk
        with self._lock:
            for key, (vk, t_ns) in list(self._keys.items()):
                if now_ns - t_ns > _HELD_CHECK_NS and not key_down(vk):
                    del self._keys[key]
            for code, t_ns in list(self._buttons.items()):
                if now_ns - t_ns > _HELD_CHECK_NS and not key_down(button_vk(code)):
                    del self._buttons[code]

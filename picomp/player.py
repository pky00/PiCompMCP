"""Replays a recording through the board (PLAN.md section 7): deadline timing,
touch detection, the echo check, auto-pause, Resume and Stop.

Every OS and board call is injected, so tests drive a Player with a fake clock,
a fake sleep and a send stub."""
import collections
import logging
import threading
import time

from picomp import board, keymap, win

log = logging.getLogger(__name__)

ECHO_TIMEOUT_NS = 300_000_000
TOUCH_WINDOW_NS = 500_000_000
NEAR_SENT_PX = 2
NEAR_LAST_PX = 10  # both are Chebyshev distances: max(|dx|, |dy|)

# time.sleep is high-resolution on Python 3.12; Event.wait and lock timeouts
# follow the 15.6 ms system tick, so they are never used as timers here.
SLICE_S = 0.005
IDLE_SLICE_S = 0.01  # while paused or resuming
LATE_LOG_NS = 10_000_000
LATE_SHIFT_NS = 100_000_000
LATE_PAUSE_NS = 1_000_000_000
STALL_NS = 1_000_000_000
ALT_TAP_S = 0.02
FOCUS_SETTLE_S = 0.05
MOVE_SETTLE_S = 0.03
_HISTORY_NS = 5_000_000_000  # bounds the sent-position history if no moves come back

REASON_TOUCH = "you touched the mouse or keyboard"
REASON_ECHO = "input hooks are not seeing the replay (admin window focused, secure desktop, or hook dropped)"
REASON_SECURE = "secure desktop"
REASON_STALLED = "input monitor stalled"
REASON_BEHIND = "replay fell behind (system stalled or slept)"
REASON_REFOCUS = "could not return to {}"
REASON_FINISHED = "replay finished"
REASON_LOST = "board disconnected"
REASON_FAILED = "replay failed: {}"

_WATCHING = ("running", "resuming")


def _near(x1, y1, x2, y2, px):
    return max(abs(x1 - x2), abs(y1 - y2)) <= px


def _within(ev_t, sent_t):
    # No lower bound: a hook event can carry a timestamp from just before the send.
    return sent_t is not None and ev_t - sent_t <= TOUCH_WINDOW_NS


def _nothing_held():
    return {}, set()


class TouchDetector:
    # The board looks like real hardware to the hooks, so an observed event is the
    # board's own only if it matches something the replay sent (PLAN.md section 7).
    # Not thread-safe: the Player calls it under its lock.

    def __init__(self, usage_for=None):
        self._usage_for = usage_for or keymap.usage_for
        self._mark = None
        self._reset(None)

    def _reset(self, cursor):
        self._positions = collections.deque()  # (t_ns, x, y) of every position sent
        self._last = cursor
        self._keys = set()  # usages the replay holds
        self._buttons = set()  # buttons the replay holds
        self._kd = {}  # usage -> t_ns of the last KD sent
        self._ku = {}
        self._bt = {}  # (button, down) -> t_ns of the last BD/BU sent
        self._wheel = None
        self._held_keys = set()  # (scan, ext) physically held at the mark
        self._held_buttons = set()

    def mark(self, t_ns, held_keys, held_buttons, cursor):
        self._reset(tuple(cursor) if cursor else None)
        self._mark = t_ns
        self._held_keys = set(held_keys)
        self._held_buttons = set(held_buttons)

    def sent_pos(self, t_ns, x, y):
        self._positions.append((t_ns, x, y))
        while t_ns - self._positions[0][0] > _HISTORY_NS:
            self._positions.popleft()
        self._last = (x, y)

    def sent_key(self, t_ns, usage, down):
        if down:
            self._keys.add(usage)
            self._kd[usage] = t_ns
        else:
            self._keys.discard(usage)
            self._ku[usage] = t_ns

    def sent_button(self, t_ns, code, down):
        self._bt[(code, down)] = t_ns
        if down:
            self._buttons.add(code)
        else:
            self._buttons.discard(code)

    def sent_wheel(self, t_ns):
        self._wheel = t_ns

    def sent_release_all(self, t_ns):
        for u in self._keys:
            self._ku[u] = t_ns
        for b in self._buttons:
            self._bt[(b, False)] = t_ns
        self._keys.clear()
        self._buttons.clear()

    def is_touch(self, ev):
        if self._mark is None or ev.kind == "hotkey" or ev.injected:
            return False
        if ev.t_ns < self._mark:
            self._before_mark(ev)
            return False
        if ev.kind == "move":
            return self._move_touch(ev)
        if ev.kind == "key":
            return self._key_touch(ev)
        if ev.kind == "button":
            return self._button_touch(ev)
        if ev.kind == "wheel":
            return not _within(ev.t_ns, self._wheel)
        return True

    def _before_mark(self, ev):
        # Still queued when the mark was taken, so the held snapshot missed it.
        if ev.kind == "key":
            if keymap.is_fake_shift(ev.scan, ev.ext, ev.vk):
                return  # the snapshot ignores them too
            held, item = self._held_keys, (ev.scan, ev.ext)
        elif ev.kind == "button":
            held, item = self._held_buttons, ev.button
        else:
            return
        if ev.down:
            held.add(item)
        else:
            held.discard(item)

    def _move_touch(self, ev):
        # Moves from the hook thread arrive in time order, so older entries can go.
        pos = self._positions
        while pos and ev.t_ns - pos[0][0] > TOUCH_WINDOW_NS:
            pos.popleft()
        for _, x, y in pos:
            if _near(ev.x, ev.y, x, y, NEAR_SENT_PX):
                return False
        if self._last is None:
            return True
        return not _near(ev.x, ev.y, self._last[0], self._last[1], NEAR_LAST_PX)

    def _key_touch(self, ev):
        if keymap.is_fake_shift(ev.scan, ev.ext, ev.vk):
            return False
        sk = (ev.scan, ev.ext)
        usage = self._usage_for(ev.scan, ev.ext)
        if ev.down:
            # Held at the mark or by the replay: Windows auto-repeat. A recent KD
            # covers hook lag after a quick KD/KU.
            if sk in self._held_keys or usage in self._keys:
                return False
            return not _within(ev.t_ns, self._kd.get(usage))
        if sk in self._held_keys:
            self._held_keys.discard(sk)
            return False
        return not _within(ev.t_ns, self._ku.get(usage))

    def _button_touch(self, ev):
        if not ev.down and ev.button in self._held_buttons:
            self._held_buttons.discard(ev.button)
            return False
        return not _within(ev.t_ns, self._bt.get((ev.button, ev.down)))


class EchoCheck:
    # Every KD, KU, BD, BU and W sent must come back through the hooks. If one does
    # not, the hooks are blind (elevated window, secure desktop, dropped hook) and
    # touch detection would be silently dead.

    def __init__(self, usage_for=None):
        self._usage_for = usage_for or keymap.usage_for
        self._pending = []  # (t_ns, kind, code), oldest first

    def expect(self, t_ns, kind, code=0):
        self._pending.append((t_ns, kind, code))

    def seen(self, ev):
        if ev.injected:
            return
        if ev.kind == "key":
            if keymap.is_fake_shift(ev.scan, ev.ext, ev.vk):
                return  # not the board's: a fake E0 36 must not stand in for Right Shift
            kind, code = ("kd" if ev.down else "ku"), self._usage_for(ev.scan, ev.ext)
        elif ev.kind == "button":
            kind, code = ("bd" if ev.down else "bu"), ev.button
        elif ev.kind == "wheel":
            kind, code = "w", None
        else:
            return
        for i, (_, k, c) in enumerate(self._pending):
            if k == kind and (kind == "w" or c == code):
                del self._pending[i]
                return

    def overdue(self, now_ns):
        return any(now_ns - t > ECHO_TIMEOUT_NS for t, _, _ in self._pending)

    def clear(self):
        self._pending.clear()


class Player:
    def __init__(self, steps, screen, send, *, clock_ns=time.perf_counter_ns, sleep=time.sleep,
                 cursor_pos=win.cursor_pos, foreground=win.foreground_window,
                 set_foreground=win.set_foreground, window_title=win.window_title,
                 held_snapshot=None, dispatcher_alive_ns=None,
                 release_fallback=win.sendinput_release, usage_for=keymap.usage_for,
                 on_pause=None, on_end=None):
        self._steps = list(steps)
        self._total = len(self._steps)
        self._w, self._h = screen
        self._send = send
        self._clock_ns = clock_ns
        self._sleep = sleep
        self._cursor_pos = cursor_pos
        self._foreground = foreground
        self._set_foreground = set_foreground
        self._window_title = window_title
        self._held_snapshot = held_snapshot or _nothing_held
        self._dispatcher_alive_ns = dispatcher_alive_ns or clock_ns  # default: always alive
        self._release_fallback = release_fallback
        self._usage_for = usage_for
        self._on_pause = on_pause
        self._on_end = on_end

        self._lock = threading.RLock()
        self._thread = None
        self._touch = TouchDetector(usage_for)
        self._echo = EchoCheck(usage_for)
        self._phase = "ready"
        self._reason = None
        self._index = 0
        self._shift = 0
        self._t_start = 0
        self._alive_ns = 0
        self._held_keys = []  # usages the replay holds, in press order
        self._held_buttons = {}  # button -> index of the bdown step that pressed it
        self._last_pos = None
        self._target = 0
        self._pause_ns = 0
        self._keys_at_pause = []
        self._resume_index = 0
        self._resume_pos = None

    @property
    def step(self):
        return self._index

    @property
    def total(self):
        return self._total

    @property
    def phase(self):
        return self._phase

    @property
    def reason(self):
        return self._reason

    @property
    def alive_ns(self):
        return self._alive_ns

    def begin(self):
        with self._lock:
            if self._phase != "ready":
                return  # stopped before it started: run() just ends
            self._t_start = self._clock_ns()
            self._alive_ns = self._t_start
            keys, buttons = self._held_snapshot()
            self._touch.mark(self._t_start, keys, buttons, self._cursor_pos())
            self._target = self._foreground() or 0
            self._phase = "running"

    def start(self):
        self.begin()
        self._thread = threading.Thread(target=self.run, name="replay", daemon=True)
        self._thread.start()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def run(self):
        try:
            if self._phase == "ready":
                self.begin()
            while self._phase != "done":
                self._alive_ns = self._clock_ns()
                if self._phase in ("paused", "resuming"):
                    self._sleep(IDLE_SLICE_S)
                    continue
                wait, reason = self._advance()
                if reason:
                    self.pause(reason)
                elif wait:
                    self._sleep(wait)
        except Exception as e:
            log.exception("replay thread failed")
            self.stop(REASON_FAILED.format(e))
        finally:
            self._call(self._on_end, self._reason)

    def _advance(self):
        # One pass of the timing loop. Returns (seconds to sleep, pause reason);
        # the caller sleeps or pauses outside the lock.
        with self._lock:
            if self._phase != "running":
                return 0, None
            if self._index >= self._total:
                self._finish()
                return 0, None
            now = self._clock_ns()
            if self._echo.overdue(now):
                return 0, REASON_ECHO
            deadline = self._t_start + self._steps[self._index].t_ms * 1_000_000 + self._shift
            if deadline > now:
                return min((deadline - now) / 1e9, SLICE_S), None
            late = now - deadline
            if late > LATE_PAUSE_NS:
                return 0, REASON_BEHIND
            if late > LATE_SHIFT_NS:
                # Keep the remaining spacing: overdue steps sent in a burst could
                # turn two clicks into a double-click.
                self._shift += late
                log.info("step %d was %d ms late; later steps shifted", self._index, late // 1_000_000)
            elif late > LATE_LOG_NS:
                log.info("step %d was %d ms late", self._index, late // 1_000_000)
            if self._cursor_pos() is None:
                return 0, REASON_SECURE
            if now - self._dispatcher_alive_ns() > STALL_NS:
                return 0, REASON_STALLED
            if not self._send_step():
                self.stop(REASON_LOST, "lost")
                return 0, None
            self._target = self._foreground() or self._target
        return 0, None

    def _finish(self):
        self._phase = "done"
        self._reason = REASON_FINISHED
        if not self._release(esc=False):
            self._reason = REASON_LOST
            self._fallback()
        self._forget_held()
        log.info("replay finished: %d steps", self._total)

    def observe(self, ev):
        if self._phase not in _WATCHING:
            return
        with self._lock:
            if self._phase not in _WATCHING:
                return
            self._echo.seen(ev)
            touch = self._touch.is_touch(ev)
        if touch:
            log.info("touch detected: %s", ev)
            self.pause(REASON_TOUCH)

    def pause(self, reason):
        with self._lock:
            if self._phase != "running":
                return False
            self._phase = "paused"
            self._reason = reason
            self._pause_ns = self._clock_ns()
            self._keys_at_pause = list(self._held_keys)
            if self._held_buttons:
                # Resume redoes the drag from its press instead of from the middle.
                self._resume_index = min(self._held_buttons.values())
                s = self._steps[self._resume_index]
                self._resume_pos = (s.x, s.y)
            else:
                self._resume_index = self._index
                self._resume_pos = self._last_pos
            ok = self._release(esc=reason != REASON_SECURE)
            self._echo.clear()
            if not ok:
                self.stop(REASON_LOST, "lost")
                return False
        log.info("replay paused at step %d of %d: %s", self._resume_index, self._total, reason)
        self._call(self._on_pause, reason)
        return True

    def resume(self):
        refused = None
        with self._lock:
            if self._phase != "paused":
                return "nothing to resume"
            self._phase = "resuming"
            keys, buttons = self._held_snapshot()
            self._touch.mark(self._clock_ns(), keys, buttons, self._cursor_pos())
            self._echo.clear()
            if self._target:
                if not self._tap_alt():
                    self.stop(REASON_LOST, "lost")
                    return REASON_LOST
                self._set_foreground(self._target)
                self._sleep(FOCUS_SETTLE_S)
                if self._foreground() != self._target:
                    refused = REASON_REFOCUS.format(self._window_title(self._target) or "the target window")
                    self._phase = "paused"
                    self._reason = refused
                    self._echo.clear()
            if not refused:
                if not self._restore():
                    self.stop(REASON_LOST, "lost")
                    return REASON_LOST
                now = self._clock_ns()
                if self._resume_index < self._total:
                    # A rewound drag or a pause for lateness must not come back late.
                    due = self._t_start + self._steps[self._resume_index].t_ms * 1_000_000
                    self._shift = max(self._shift + (now - self._pause_ns), now - due)
                self._index = self._resume_index
                self._reason = None
                self._phase = "running"
        if refused:
            log.info("resume refused: %s", refused)
            self._call(self._on_pause, refused)
            return refused
        log.info("replay resumed at step %d of %d", self._index, self._total)
        return None

    def _tap_alt(self):
        # An Alt press lifts Windows' foreground lock; without it SetForegroundWindow
        # would leave focus on the browser that sent Resume.
        if not self._key(True, keymap.LALT):
            return False
        self._sleep(ALT_TAP_S)
        ok = self._key(False, keymap.LALT)
        self._sleep(ALT_TAP_S)
        return ok

    def _restore(self):
        if self._resume_pos:
            if not self._move(self._resume_pos[0], self._resume_pos[1]):
                return False
            self._sleep(MOVE_SETTLE_S)
        for u in self._keys_at_pause:
            if not self._press(u):
                return False
        return True

    def stop(self, reason, how="normal"):
        with self._lock:
            if self._phase == "done":
                return
            was_running = self._phase == "running"
            self._phase = "done"
            self._reason = reason
            # "released": the board already let go after !HALT or !TIMEOUT, so no Esc.
            if how == "lost" or not self._release(esc=(how == "normal" and was_running)):
                self._fallback()
            self._forget_held()
            self._echo.clear()
        log.info("replay stopped at step %d of %d: %s", self._index, self._total, reason)

    def _call(self, fn, reason):
        if fn:
            try:
                fn(self, reason)
            except Exception:
                log.exception("replay callback failed")

    # Everything below runs under self._lock.

    def _send_step(self):
        i = self._index
        s = self._steps[i]
        self._index = i + 1
        if s.kind == "move":
            return self._move(s.x, s.y)
        if s.kind == "bdown":
            if s.code in self._held_buttons:
                return self._move(s.x, s.y)
            return self._button(True, s.code, s.x, s.y, i)
        if s.kind == "bup":
            if s.code not in self._held_buttons:
                return self._move(s.x, s.y)
            return self._button(False, s.code, s.x, s.y)
        if s.kind == "wheel":
            return self._wheel(s.wheel_v or 0, s.wheel_h or 0, s.x, s.y)
        if s.kind == "kdown":
            return self._press(s.code)
        if s.kind == "kup":
            if s.code not in self._held_keys:
                return True
            return self._key(False, s.code)
        log.warning("step %d has unknown kind %r; skipped", i, s.kind)
        return True

    def _tx(self, line):
        if self._send(line):
            return True
        log.warning("board did not take %r", line)
        return False

    def _hid(self, x, y):
        return win.px_to_hid(x, self._w), win.px_to_hid(y, self._h)

    def _move(self, x, y):
        hx, hy = self._hid(x, y)
        self._touch.sent_pos(self._clock_ns(), x, y)
        self._last_pos = (x, y)
        return self._tx(board.fmt_move(hx, hy))

    def _button(self, down, code, x, y, index=None):
        hx, hy = self._hid(x, y)
        line = board.fmt_button(down, code, hx, hy)
        t = self._clock_ns()
        self._echo.expect(t, "bd" if down else "bu", code)
        self._touch.sent_button(t, code, down)
        self._touch.sent_pos(t, x, y)
        self._last_pos = (x, y)
        ok = self._tx(line)
        if down:
            self._held_buttons[code] = index
        elif ok:
            del self._held_buttons[code]  # a failed BU keeps it for the SendInput fallback
        return ok

    def _wheel(self, v, h, x, y):
        hx, hy = self._hid(x, y)
        line = board.fmt_wheel(v, h, hx, hy)
        t = self._clock_ns()
        self._echo.expect(t, "w")
        self._touch.sent_wheel(t)
        self._touch.sent_pos(t, x, y)
        self._last_pos = (x, y)
        return self._tx(line)

    def _key(self, down, usage):
        line = board.fmt_key(down, usage)
        t = self._clock_ns()
        self._echo.expect(t, "kd" if down else "ku", usage)
        self._touch.sent_key(t, usage, down)
        ok = self._tx(line)
        if down:
            self._held_keys.append(usage)
        elif ok:
            self._held_keys.remove(usage)
        return ok

    def _holds_any(self, usages):
        return any(u in self._held_keys for u in usages)

    def _press(self, usage):
        # kdown with its skip rules; True when nothing had to be sent.
        if usage in self._held_keys:
            return True
        if (usage in (keymap.ESC, keymap.KEY_R)
                and self._holds_any(keymap.CTRL_USAGES) and self._holds_any(keymap.ALT_USAGES)):
            # Ctrl+Alt+Esc and Ctrl+Alt+R are PiComp's own hotkeys.
            log.info("skipped key %d: the replay holds Ctrl and Alt", usage)
            return True
        return self._key(True, usage)

    def _user_holds_ctrl_alt(self):
        keys, _ = self._held_snapshot()
        return any(sk in keymap.CTRL_ALT_SCANS and self._usage_for(sk[0], sk[1]) not in self._held_keys
                   for sk in keys)

    def _esc_rule(self):
        # Esc cancels a drag-and-drop or a window move instead of dropping it at a
        # midpoint. Not while you hold Ctrl or Alt: Ctrl+Esc and Alt+Esc are shortcuts.
        # Not on the secure desktop (GetCursorPos fails there) either: the board's
        # Esc would answer "No" to a UAC prompt. R alone still releases everything.
        if not self._held_buttons or self._user_holds_ctrl_alt() or self._cursor_pos() is None:
            return True
        ok = True
        for u in reversed(list(self._held_keys)):
            ok = self._key(False, u) and ok
        return ok and self._key(True, keymap.ESC) and self._key(False, keymap.ESC)

    def _release(self, esc):
        # On failure the held state is kept so the caller can fall back to SendInput.
        if esc and not self._esc_rule():
            return False
        if not self._tx(board.RELEASE):
            return False
        self._touch.sent_release_all(self._clock_ns())
        self._forget_held()
        return True

    def _fallback(self):
        # Windows can keep a vanished device's keys and buttons down, so release
        # exactly what the replay held.
        keys = [sk for sk in (keymap.send_scan_for(u) for u in self._held_keys) if sk]
        buttons = list(self._held_buttons)
        if not (keys or buttons):
            return
        try:
            self._release_fallback(keys, buttons)
        except Exception:
            log.exception("SendInput release failed")

    def _forget_held(self):
        self._held_keys.clear()
        self._held_buttons.clear()

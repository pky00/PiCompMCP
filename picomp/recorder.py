"""Turns hook events into recording steps (dispatcher thread), plus the screenshot worker.

Steps and screenshots stay in memory until stop; the controller writes them to
SQLite in one transaction after finish().
"""
import collections
import logging
import queue
import threading
import time

from picomp import keymap, win
from picomp.db import Step

log = logging.getLogger(__name__)

MOVE_INTERVAL_NS = 16_000_000  # about 60 Hz
MISSED_UP_NS = 250_000_000
WHEEL_DELTA = 120  # one notch
MAX_NOTCHES = 127  # per wheel step, the wire limit

RecordResult = collections.namedtuple("RecordResult", "steps injected unmapped discard_reason")

DISCARD_ELSEWHERE = "you clicked or scrolled outside the main screen, which the board cannot reach"


def _no_shot(_key):
    pass


def _clamp(v, size):
    return max(0, min(size - 1, v))


def _notches(acc):
    # Whole notches, truncated toward 0 so the remainder keeps its sign.
    n = abs(acc) // WHEEL_DELTA
    return -n if acc < 0 else n


def _chunks(n):
    out = []
    while n:
        c = max(-MAX_NOTCHES, min(MAX_NOTCHES, n))
        out.append(c)
        n -= c
    return out


class Recorder:
    def __init__(self, t0_ns, display, shots_mode, request_shot=None, key_down=None, button_vk=None):
        self.t0_ns = t0_ns
        self.display = display
        self.shots_mode = shots_mode
        self.discard_reason = None
        self.injected = 0
        self.unmapped = 0
        self._request_shot = request_shot or _no_shot
        self._key_down = key_down or win.key_down
        self._button_vk = button_vk or win.button_vk
        self._w, self._h = display[0], display[1]
        # The other screens: Windows maps the board's absolute mouse onto the main one only.
        self._elsewhere = [s for s in display[3] if s != (0, 0, self._w, self._h)]
        self._away = False  # the pointer was last seen on another screen
        self._steps = []
        self._last_t_ms = 0
        self._pending = None  # newest unwritten move: (t_ms, x, y, t_ns)
        self._last_move_ns = None  # hook time of the last written move
        self._pos = None  # last clamped mouse position seen
        self._keys = {}  # usage -> (vk, t_ns of the down)
        self._buttons = {}  # code -> t_ns of the down
        self._acc_v = 0
        self._acc_h = 0
        self._clean_downs = []  # seqs of downs appended while nothing was held
        if shots_mode == "start_end":
            self._request_shot(0)

    @property
    def step_count(self):
        return len(self._steps)

    def discard(self, reason):
        if self.discard_reason is None:
            self.discard_reason = reason

    def feed(self, ev):
        if ev.kind == "hotkey" or ev.t_ns <= self.t0_ns:
            return
        if ev.injected:
            self.injected += 1
            return
        t_ms = (ev.t_ns - self.t0_ns) // 1_000_000
        if ev.kind == "move":
            self._move(ev, t_ms)
        elif ev.kind == "button":
            self._button(ev, t_ms)
        elif ev.kind == "wheel":
            self._wheel(ev, t_ms)
        elif ev.kind == "key":
            self._key(ev, t_ms)

    def tick(self, now_ns):
        # Inserts the ups the hooks never saw (UAC prompt, Win+L, Alt+Tab into an admin window).
        t_ms = max(0, (now_ns - self.t0_ns) // 1_000_000)
        for usage, (vk, t_ns) in list(self._keys.items()):
            if now_ns - t_ns > MISSED_UP_NS and not self._key_down(vk):
                log.info("key-up of usage %d not seen by the hook; inserted at %d ms", usage, t_ms)
                self._append("kup", t_ms, code=usage)
                del self._keys[usage]
        for code, t_ns in list(self._buttons.items()):
            if now_ns - t_ns > MISSED_UP_NS and not self._key_down(self._button_vk(code)):
                if self._away:
                    # Let go on another screen (its up still queued, or lost): that drop
                    # cannot be replayed.
                    self.discard(DISCARD_ELSEWHERE)
                    del self._buttons[code]
                    continue
                log.info("button-up of button %d not seen by the hook; inserted at %d ms", code, t_ms)
                x, y = self._pos
                self._append("bup", t_ms, x, y, code)
                del self._buttons[code]

    def finish(self):
        self._write_pending()
        steps = list(self._steps)
        if self._clean_downs:
            # The stop (or shutdown) chord starts with the last down made while nothing
            # was held: its Ctrl and Alt (or the Ctrl of Ctrl+C) go, with everything after.
            steps = steps[:self._clean_downs[-1]]
        while steps and steps[-1].kind == "move":
            steps.pop()
        if self.shots_mode == "start_end":
            self._request_shot("end")
        return RecordResult(steps, self.injected, self.unmapped, self.discard_reason)

    def _held_nothing(self):
        return not self._keys and not self._buttons

    def _point(self, ev):
        self._pos = (_clamp(ev.x, self._w), _clamp(ev.y, self._h))
        return self._pos

    def _on_other_screen(self, ev):
        # A point outside every screen is a move pushed past an outer edge: it belongs
        # to the screen the pointer was last on, and past the main screen's own edge
        # it is clamped onto it.
        x, y = ev.x, ev.y
        if 0 <= x < self._w and 0 <= y < self._h:
            self._away = False
        elif any(left <= x < right and top <= y < bottom for left, top, right, bottom in self._elsewhere):
            self._away = True
        return self._away

    def _append(self, kind, t_ms, x=None, y=None, code=None, wheel_v=None, wheel_h=None):
        if kind != "move":
            self._write_pending()
        # The two hook threads share one queue, so stamps can arrive slightly out of order.
        t_ms = max(t_ms, self._last_t_ms)
        self._last_t_ms = t_ms
        seq = len(self._steps)
        self._steps.append(Step(seq, t_ms, kind, x, y, code, wheel_v, wheel_h))
        return seq

    def _write_pending(self):
        if self._pending is None:
            return
        t_ms, x, y, t_ns = self._pending
        self._pending = None
        self._last_move_ns = t_ns
        self._append("move", t_ms, x, y)

    def _move(self, ev, t_ms):
        if self._on_other_screen(ev):
            self._write_pending()  # the replay leaves the pointer where it left the main screen
            return
        x, y = self._point(ev)
        self._pending = (t_ms, x, y, ev.t_ns)
        if self._last_move_ns is None or ev.t_ns - self._last_move_ns >= MOVE_INTERVAL_NS:
            self._write_pending()

    def _button(self, ev, t_ms):
        code = ev.button
        if not 1 <= code <= 5:
            return
        if self._on_other_screen(ev):
            if ev.down or code in self._buttons:
                self.discard(DISCARD_ELSEWHERE)
            return  # else an up whose press was not recorded
        x, y = self._point(ev)
        if ev.down:
            if code in self._buttons:
                return
            clean = self._held_nothing()
            seq = self._append("bdown", t_ms, x, y, code)
            self._buttons[code] = ev.t_ns
            if clean:
                self._clean_downs.append(seq)
            if self.shots_mode == "each_click":
                self._request_shot(seq)
        elif code in self._buttons:
            self._append("bup", t_ms, x, y, code)
            del self._buttons[code]

    def _wheel(self, ev, t_ms):
        if self._on_other_screen(ev):
            self.discard(DISCARD_ELSEWHERE)
            return
        x, y = self._point(ev)
        self._acc_v += ev.dv
        self._acc_h += ev.dh
        nv = _notches(self._acc_v)
        nh = _notches(self._acc_h)
        self._acc_v -= nv * WHEEL_DELTA
        self._acc_h -= nh * WHEEL_DELTA
        for n in _chunks(nv):
            self._append("wheel", t_ms, x, y, wheel_v=n, wheel_h=0)
        for n in _chunks(nh):
            self._append("wheel", t_ms, x, y, wheel_v=0, wheel_h=n)

    def _key(self, ev, t_ms):
        if keymap.is_fake_shift(ev.scan, ev.ext, ev.vk):
            return
        usage = keymap.usage_for(ev.scan, ev.ext)
        if usage is None:
            self.unmapped += 1
            log.info("unmapped key dropped: scan 0x%02X ext %s vk 0x%02X", ev.scan, ev.ext, ev.vk)
            return
        if ev.down:
            if usage in self._keys:
                return  # Windows auto-repeat
            clean = self._held_nothing()
            seq = self._append("kdown", t_ms, code=usage)
            self._keys[usage] = (ev.vk, ev.t_ns)
            if clean:
                self._clean_downs.append(seq)
        elif usage in self._keys:
            self._append("kup", t_ms, code=usage)
            del self._keys[usage]


def final_shots(mode, shots, n):
    # Maps the worker's results onto the kept steps: each_click shots of cut
    # bdowns are dropped; start_end becomes seq 0 and seq n.
    if mode == "each_click":
        return {k: v for k, v in shots.items() if isinstance(k, int) and 0 <= k < n}
    if mode == "start_end":
        out = {}
        if 0 in shots:
            out[0] = shots[0]
        if "end" in shots:
            out[n] = shots["end"]
        return out
    return {}


class ShotWorker:
    # Grabs screenshots on its own thread so the dispatcher never waits for mss.

    def __init__(self, grab=None):
        self._grab = grab or win.screenshot_png
        self._q = queue.SimpleQueue()
        self._cond = threading.Condition()
        self._gen = 0
        self._pending = 0
        self._results = {}
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="shots", daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._q.put(None)
        self._thread.join(2)
        if self._thread.is_alive():
            log.warning("screenshot worker did not stop within 2 s")
        self._thread = None

    def begin(self):
        with self._cond:
            self._gen += 1
            self._pending = 0
            self._results = {}
            self._cond.notify_all()

    def request(self, key):
        with self._cond:
            self._pending += 1
            gen = self._gen
        self._q.put((gen, key))

    def drain(self, timeout=10.0):
        deadline = time.perf_counter() + timeout
        with self._cond:
            gen = self._gen
            while self._pending and self._gen == gen:
                left = deadline - time.perf_counter()
                if left <= 0:
                    log.warning("%d screenshots still pending after %.0f s; saving without them",
                                self._pending, timeout)
                    break
                self._cond.wait(left)
            if self._gen != gen:
                return {}
            return dict(self._results)

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            gen, key = item
            with self._cond:
                if gen != self._gen:
                    continue  # requested before the last begin(): nobody wants it
            try:
                png = self._grab()
            except Exception:
                log.exception("screenshot %s failed", key)
                png = None
            with self._cond:
                if gen != self._gen:
                    continue
                if png is None:
                    log.warning("screenshot %s failed; skipped", key)
                else:
                    self._results[key] = png
                self._pending -= 1
                self._cond.notify_all()

"""Player: timing, sending, pause, resume and stop (PLAN.md section 7), plus the
echo check. A fake clock and sleep drive the replay synchronously; the send stub
records each line and plays its hook echo back on the next sleep, as the
dispatcher would."""
import pytest

from picomp import keymap
from picomp.db import Step
from picomp.hooks import button_ev, key_ev, move_ev, wheel_ev
from picomp.player import (REASON_BEHIND, REASON_ECHO, REASON_FINISHED, REASON_LOST, REASON_SECURE,
                           REASON_STALLED, REASON_TOUCH, EchoCheck, Player)

MS = 1_000_000
T_START = 5_000 * MS
SCREEN = (32768, 32768)  # px_to_hid is the identity at this size, so the lines read in pixels
TARGET = 0x10010
BROWSER = 0x20020
LIMIT_NS = 60_000 * MS  # a replay still going after a fake minute is stuck


class Stuck(Exception):
    pass


def move(t, x, y):
    return Step(0, t, "move", x, y)


def bdown(t, code, x, y):
    return Step(0, t, "bdown", x, y, code)


def bup(t, code, x, y):
    return Step(0, t, "bup", x, y, code)


def wheel(t, v, h, x, y):
    return Step(0, t, "wheel", x, y, None, v, h)


def kdown(t, usage):
    return Step(0, t, "kdown", code=usage)


def kup(t, usage):
    return Step(0, t, "kup", code=usage)


def echo_events(t_ns, line):
    # What the hooks report for a line the board carried out. R gets none: its
    # ups only arrive once the replay has stopped watching.
    cmd, *args = line.split()
    a = [int(v) for v in args]
    if cmd == "M":
        return [move_ev(t_ns, a[0], a[1])]
    if cmd in ("BD", "BU"):
        return [button_ev(t_ns, a[0], cmd == "BD", a[1], a[2])]
    if cmd == "W":
        return [wheel_ev(t_ns, a[0] * 120, a[1] * 120, a[2], a[3])]
    if cmd in ("KD", "KU"):
        scan, ext = keymap.scan_for(a[0])
        # Right Shift's vk is what tells it from a fake E0 36.
        vk = keymap.VK_RSHIFT if a[0] == keymap.RSHIFT else 0
        return [key_ev(t_ns, scan, ext, cmd == "KD", vk=vk)]
    return []


class Rig:
    # One Player with a fake clock, OS and board. sleep() advances the clock,
    # hands the queued echoes to observe(), then runs the actions due by then.

    def __init__(self, steps, echo=True, fg=TARGET):
        self.now = T_START
        self.echo = echo
        self.fg = fg
        self.cursor = (0, 0)
        self.held = ({}, set())
        self.title = "Notepad"
        self.focus_ok = True
        self.alive_ns = None
        self.fail = None
        self.jumps = {}  # sent line index -> ns the clock jumps while that write blocks
        self.sent = []  # (ms since start, line)
        self.slept = []
        self.queued = []
        self.actions = []
        self.focus_calls = []
        self.fallbacks = []
        self.pauses = []
        self.ends = []
        self.results = []
        self.player = Player(
            [s._replace(seq=i) for i, s in enumerate(steps)], SCREEN, self.send,
            clock_ns=self.clock, sleep=self.sleep, cursor_pos=lambda: self.cursor,
            foreground=lambda: self.fg, set_foreground=self.set_foreground,
            window_title=lambda hwnd: self.title, held_snapshot=lambda: self.held,
            dispatcher_alive_ns=self.dispatcher_alive,
            release_fallback=lambda keys, buttons: self.fallbacks.append((keys, buttons)),
            on_pause=lambda p, reason: self.pauses.append(reason),
            on_end=lambda p, reason: self.ends.append(reason))

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += round(seconds * 1e9)
        if self.now - T_START > LIMIT_NS:
            raise Stuck("the replay never ended")
        events, self.queued = self.queued, []
        for ev in events:
            self.player.observe(ev)
        while self.actions and self.actions[0][0] <= self.now:
            _, fn = self.actions.pop(0)
            fn()

    def send(self, line):
        t = self.now
        self.sent.append(((t - T_START) / MS, line))
        self.now += self.jumps.pop(len(self.sent) - 1, 0)
        if self.fail and self.fail(line):
            return False
        if self.echo:
            self.queued.extend(echo_events(t, line))
        return True

    def set_foreground(self, hwnd):
        self.focus_calls.append(hwnd)
        if self.focus_ok:
            self.fg = hwnd
        return self.fg == hwnd

    def dispatcher_alive(self):
        return self.now if self.alive_ns is None else self.alive_ns

    def at(self, ms, fn):
        self.actions.append((T_START + ms * MS, fn))
        self.actions.sort(key=lambda a: a[0])

    def stop_at(self, ms, reason="test over", how="normal"):
        self.at(ms, lambda: self.player.stop(reason, how))

    def press_a(self):
        # A key the replay did not send: you touched the keyboard.
        self.player.observe(key_ev(self.now, 0x1E, False, True, vk=0x41))

    def resume(self):
        self.results.append(self.player.resume())

    def run(self):
        self.player.begin()
        self.player.run()
        return self.sent


# A shift-drag: Shift held, button 1 pressed at (10, 10), dragged to (90, 90).
DRAG = [move(0, 10, 10), kdown(10, 225), bdown(20, 1, 10, 10), move(40, 50, 50),
        move(60, 90, 90), bup(80, 1, 90, 90), kup(90, 225)]
DRAG_START = [(0, "M 10 10"), (10, "KD 225"), (20, "BD 1 10 10"), (40, "M 50 50")]
ESC_RULE = [(50, "KU 225"), (50, "KD 41"), (50, "KU 41"), (50, "R")]


def test_steps_go_out_on_time_and_r_ends_the_replay():
    rig = Rig([move(0, 100, 200), bdown(50, 1, 100, 200), bup(120, 1, 110, 210),
               wheel(200, -2, 0, 110, 210), kdown(300, 4), kup(380, 4), move(400, 300, 400)])
    assert rig.run() == [
        (0, "M 100 200"), (50, "BD 1 100 200"), (120, "BU 1 110 210"), (200, "W -2 0 110 210"),
        (300, "KD 4"), (380, "KU 4"), (400, "M 300 400"), (400, "R")]
    assert max(rig.slept) <= 0.005
    assert rig.pauses == []
    assert rig.ends == [REASON_FINISHED]
    assert (rig.player.phase, rig.player.step, rig.player.total) == ("done", 7, 7)
    assert rig.fallbacks == []


def test_redundant_steps_are_skipped_or_sent_as_moves():
    rig = Rig([kdown(0, 4), kdown(10, 4), kup(20, 4), kup(30, 4),
               bdown(40, 1, 5, 5), bdown(50, 1, 6, 6), bup(60, 1, 7, 7), bup(70, 1, 8, 8)])
    assert rig.run() == [(0, "KD 4"), (20, "KU 4"), (40, "BD 1 5 5"), (50, "M 6 6"),
                         (60, "BU 1 7 7"), (70, "M 8 8"), (70, "R")]
    assert rig.player.step == 8


@pytest.mark.parametrize("steps, expected", [
    ([kdown(0, 224), kdown(10, 226), kdown(20, 41), kdown(30, 21), kup(40, 41), kup(50, 226),
      kup(60, 224), kdown(70, 41), kup(80, 41)],
     [(0, "KD 224"), (10, "KD 226"), (50, "KU 226"), (60, "KU 224"), (70, "KD 41"), (80, "KU 41"),
      (80, "R")]),
    ([kdown(0, 228), kdown(10, 230), kdown(20, 21), kup(30, 21), kup(40, 230), kup(50, 228)],
     [(0, "KD 228"), (10, "KD 230"), (40, "KU 230"), (50, "KU 228"), (50, "R")]),
    ([kdown(0, 224), kdown(10, 41), kup(20, 41), kup(30, 224)],
     [(0, "KD 224"), (10, "KD 41"), (20, "KU 41"), (30, "KU 224"), (30, "R")]),
], ids=["left-ctrl-alt", "right-ctrl-alt", "ctrl-only"])
def test_esc_and_r_are_never_sent_while_the_replay_holds_ctrl_and_alt(steps, expected):
    rig = Rig(steps)
    assert rig.run() == expected
    assert rig.ends == [REASON_FINISHED]


@pytest.mark.parametrize("stall_ms, expected", [
    (150, [(0, "M 1 1"), (100, "M 2 2"), (250, "M 3 3"), (300, "M 4 4"), (300, "R")]),
    (250, [(0, "M 1 1"), (100, "M 2 2"), (350, "M 3 3"), (450, "M 4 4"), (450, "R")]),
], ids=["50ms-late-logged-only", "150ms-late-rest-shifted"])
def test_late_steps(stall_ms, expected):
    rig = Rig([move(0, 1, 1), move(100, 2, 2), move(200, 3, 3), move(300, 4, 4)])
    rig.jumps[1] = stall_ms * MS  # the write of the second step blocks
    assert rig.run() == expected
    assert rig.pauses == []
    assert rig.ends == [REASON_FINISHED]


def test_more_than_one_second_late_pauses():
    rig = Rig([move(0, 1, 1), move(100, 2, 2), move(200, 3, 3)])
    rig.jumps[1] = 1200 * MS
    rig.stop_at(1400)
    assert rig.run() == [(0, "M 1 1"), (100, "M 2 2"), (1300, "R"), (1400, "R")]
    assert rig.pauses == [REASON_BEHIND]
    assert rig.ends == ["test over"]


def test_secure_desktop_pauses_before_the_step():
    rig = Rig([move(0, 1, 1), move(100, 2, 2)])
    rig.at(50, lambda: setattr(rig, "cursor", None))
    rig.stop_at(200)
    assert rig.run() == [(0, "M 1 1"), (100, "R"), (200, "R")]
    assert rig.pauses == [REASON_SECURE]


def test_a_stalled_dispatcher_pauses_before_the_step():
    rig = Rig([move(0, 1, 1), move(1001, 2, 2)])
    rig.alive_ns = T_START
    rig.stop_at(1101)  # the paused loop sleeps in 10 ms slices from 1001
    assert rig.run() == [(0, "M 1 1"), (1001, "R"), (1101, "R")]
    assert rig.pauses == [REASON_STALLED]


def test_a_missing_echo_pauses_after_300_ms():
    rig = Rig([kdown(0, 4), kup(1000, 4)], echo=False)
    rig.stop_at(405)
    assert rig.run() == [(0, "KD 4"), (305, "R"), (405, "R")]
    assert rig.pauses == [REASON_ECHO]


def test_touch_mid_drag_cancels_it_with_esc_and_resume_redoes_it_from_the_press():
    rig = Rig(DRAG)
    rig.at(50, rig.press_a)
    rig.at(200, rig.resume)
    assert rig.run() == DRAG_START + ESC_RULE + [
        (200, "KD 226"), (220, "KU 226"),  # the Alt tap lifts the foreground lock
        (290, "M 10 10"),  # back to the drag's press
        (320, "KD 225"),  # Shift again, 30 ms after the move
        (320, "BD 1 10 10"), (340, "M 50 50"), (360, "M 90 90"), (380, "BU 1 90 90"),
        (390, "KU 225"), (390, "R")]
    assert rig.pauses == [REASON_TOUCH]
    assert rig.results == [None]
    assert rig.focus_calls == [TARGET]
    assert rig.ends == [REASON_FINISHED]


def test_no_esc_when_you_hold_ctrl():
    rig = Rig(DRAG)

    def press_ctrl():
        rig.held = ({(0x1D, False): 0xA2}, set())
        rig.player.observe(key_ev(rig.now, 0x1D, False, True, vk=0xA2))

    rig.at(50, press_ctrl)
    rig.stop_at(100)
    assert rig.run() == DRAG_START + [(50, "R"), (100, "R")]
    assert rig.pauses == [REASON_TOUCH]


def test_the_replays_own_ctrl_does_not_block_the_esc_rule():
    rig = Rig([kdown(0, 224), bdown(10, 1, 5, 5), move(30, 6, 6), bup(60, 1, 6, 6), kup(70, 224)])

    def touch():
        # The hooks see the board's Ctrl as physically held, next to your A.
        rig.held = ({(0x1D, False): 0xA2, (0x1E, False): 0x41}, set())
        rig.press_a()

    rig.at(40, touch)
    rig.stop_at(100)
    assert rig.run() == [(0, "KD 224"), (10, "BD 1 5 5"), (30, "M 6 6"),
                         (40, "KU 224"), (40, "KD 41"), (40, "KU 41"), (40, "R"), (100, "R")]
    assert rig.pauses == [REASON_TOUCH]


@pytest.mark.parametrize("touch, paused_ms, reason", [
    (False, 60, REASON_SECURE),  # the cursor check before the next step
    (True, 50, REASON_TOUCH),  # any other pause while GetCursorPos fails
], ids=["cursor-check", "touch-while-blind"])
def test_no_esc_while_the_secure_desktop_has_the_input(touch, paused_ms, reason):
    # Mid-drag a UAC prompt opens: the board's Esc would land there and answer "No".
    rig = Rig(DRAG)

    def uac_prompt():
        rig.cursor = None
        if touch:
            rig.press_a()

    rig.at(50, uac_prompt)
    rig.stop_at(100)
    assert rig.run() == DRAG_START + [(paused_ms, "R"), (100, "R")]
    assert rig.pauses == [reason]


def test_pause_without_a_held_button_sends_only_r_and_resume_presses_the_keys_again():
    rig = Rig([kdown(0, 4), move(50, 5, 5), kup(100, 4)], fg=0)
    lag = []
    rig.at(30, lambda: rig.player.observe(move_ev(rig.now, 900, 900)))  # you grabbed the mouse
    rig.at(60, rig.press_a)  # already paused: ignored
    rig.at(90, lambda: lag.append(rig.now - rig.player.alive_ns))
    rig.at(100, rig.resume)
    assert rig.run() == [(0, "KD 4"), (30, "R"), (100, "KD 4"), (120, "M 5 5"),
                         (170, "KU 4"), (170, "R")]
    assert rig.pauses == [REASON_TOUCH]
    assert rig.results == [None]
    assert rig.focus_calls == []  # no target window, so no Alt tap and no refocus
    assert lag[0] <= 10 * MS  # alive_ns keeps moving while paused


@pytest.mark.parametrize("title, reason", [
    ("Notepad", "could not return to Notepad"),
    ("", "could not return to the target window"),
])
def test_resume_stays_paused_when_the_target_window_cannot_be_refocused(title, reason):
    rig = Rig([kdown(0, 4), move(100, 5, 5), kup(200, 4)])
    rig.title = title

    def click_browser():
        rig.fg = BROWSER
        rig.focus_ok = False
        rig.player.observe(button_ev(rig.now, 1, True, 900, 900))

    def retry():
        rig.focus_ok = True
        rig.resume()

    rig.at(50, click_browser)
    rig.at(100, rig.resume)
    rig.at(300, retry)
    assert rig.run() == [(0, "KD 4"), (50, "R"),
                         (100, "KD 226"), (120, "KU 226"),  # refused: no move, no re-press
                         (300, "KD 226"), (320, "KU 226"), (390, "KD 4"),
                         (440, "M 5 5"), (540, "KU 4"), (540, "R")]
    assert rig.results == [reason, None]
    assert rig.pauses == [REASON_TOUCH, reason]
    assert rig.focus_calls == [TARGET, TARGET]
    assert rig.ends == [REASON_FINISHED]


def test_resume_keeps_the_wait_that_was_left_at_the_pause():
    rig = Rig([move(0, 1, 1), move(100, 2, 2), move(200, 3, 3)], fg=0)
    rig.at(50, rig.press_a)
    rig.at(1000, rig.resume)
    # Paused 50 ms before step 1 was due; it goes out 50 ms after the resume settled.
    assert rig.run() == [(0, "M 1 1"), (50, "R"), (1000, "M 1 1"), (1080, "M 2 2"),
                         (1180, "M 3 3"), (1180, "R")]
    assert rig.results == [None]


def test_resume_after_falling_behind_does_not_come_back_late():
    rig = Rig([move(0, 1, 1), move(100, 2, 2), move(200, 3, 3)], fg=0)
    rig.jumps[0] = 1500 * MS
    rig.at(1600, rig.resume)
    assert rig.run() == [(0, "M 1 1"), (1500, "R"), (1600, "M 1 1"), (1630, "M 2 2"),
                         (1730, "M 3 3"), (1730, "R")]
    assert rig.pauses == [REASON_BEHIND]
    assert rig.ends == [REASON_FINISHED]


@pytest.mark.parametrize("how, tail, fallbacks", [
    ("normal", ESC_RULE, []),  # the Stop button and Ctrl+Alt+Esc
    ("released", [(50, "R")], []),  # after !HALT or !TIMEOUT the board already let go
    ("lost", [], [([(0x2A, False)], [1])]),  # the board vanished: SendInput ups instead
])
def test_stop_mid_drag(how, tail, fallbacks):
    rig = Rig(DRAG)
    rig.stop_at(50, "replay stopped", how)
    assert rig.run() == DRAG_START + tail
    assert rig.fallbacks == fallbacks
    assert rig.pauses == []
    assert rig.ends == ["replay stopped"]
    rig.player.stop("stopped again")
    assert rig.sent == DRAG_START + tail
    assert rig.player.reason == "replay stopped"


def test_right_shift_echo_is_seen():
    # Held past the 300 ms echo timeout, so a missed echo would pause the replay.
    rig = Rig([kdown(0, 229), kdown(10, 4), kup(20, 4), kup(400, 229)])
    assert rig.run() == [(0, "KD 229"), (10, "KD 4"), (20, "KU 4"), (400, "KU 229"), (400, "R")]
    assert rig.pauses == []
    assert rig.ends == [REASON_FINISHED]


def test_the_shift_windows_lifts_around_a_keypad_key_is_not_a_touch():
    # NumLock on: Windows lifts the replay's Right Shift around keypad 4 (scan 0x236),
    # then presses it again with the plain scan code.
    rig = Rig([kdown(0, 229), kdown(10, 0x5C), kup(20, 0x5C), kup(30, 229)])
    rig.at(10, lambda: rig.player.observe(key_ev(rig.now, 0x236, True, False, vk=0xA1)))
    rig.at(20, lambda: rig.player.observe(key_ev(rig.now, 0x36, True, True, vk=0xA1)))
    assert rig.run() == [(0, "KD 229"), (10, "KD 92"), (20, "KU 92"), (30, "KU 229"), (30, "R")]
    assert rig.pauses == []
    assert rig.ends == [REASON_FINISHED]


def test_a_fake_right_shift_is_not_an_echo():
    rig = Rig([kdown(0, 229), kup(1000, 229)], echo=False)
    rig.at(5, lambda: rig.player.observe(key_ev(rig.now, 0x36, True, True, vk=0xFF)))
    rig.stop_at(405)
    assert rig.run() == [(0, "KD 229"), (305, "R"), (405, "R")]
    assert rig.pauses == [REASON_ECHO]


def test_a_lost_board_holding_right_shift_releases_it_as_plain_36():
    # SendInput applies the layout again; E0 36 would release nothing.
    rig = Rig([move(0, 10, 10), kdown(10, 229), bdown(20, 1, 10, 10), move(40, 50, 50),
               move(60, 90, 90), bup(80, 1, 90, 90), kup(90, 229)])
    rig.stop_at(50, "replay stopped", "lost")
    rig.run()
    assert rig.fallbacks == [([(0x36, False)], [1])]


def test_stop_while_paused_sends_only_r():
    rig = Rig(DRAG)
    rig.at(50, rig.press_a)
    rig.stop_at(100, "replay killed (Ctrl+Alt+Esc)")
    assert rig.run() == DRAG_START + ESC_RULE + [(100, "R")]
    assert rig.ends == ["replay killed (Ctrl+Alt+Esc)"]


def test_stop_before_the_replay_thread_starts():
    rig = Rig(DRAG)
    rig.player.stop("replay stopped")
    assert rig.run() == [(0, "R")]
    assert rig.ends == ["replay stopped"]
    assert rig.player.step == 0


def test_a_failed_send_stops_and_releases_through_sendinput():
    rig = Rig([kdown(0, 4), bdown(10, 1, 5, 5), move(20, 6, 6), kup(30, 4)])
    rig.fail = lambda line: line == "M 6 6"
    assert rig.run() == [(0, "KD 4"), (10, "BD 1 5 5"), (20, "M 6 6")]
    assert rig.fallbacks == [([(0x1E, False)], [1])]
    assert rig.ends == [REASON_LOST]


def test_a_failed_final_r_falls_back_to_sendinput():
    rig = Rig([kdown(0, 4)])
    rig.fail = lambda line: line == "R"
    assert rig.run() == [(0, "KD 4"), (0, "R")]
    assert rig.fallbacks == [([(0x1E, False)], [])]
    assert rig.ends == [REASON_LOST]


def test_pause_and_resume_need_the_right_phase():
    rig = Rig([move(0, 1, 1)])
    assert rig.player.pause("x") is False
    assert rig.player.resume() == "nothing to resume"
    rig.run()
    assert rig.player.pause("x") is False
    assert rig.player.resume() == "nothing to resume"
    assert rig.pauses == []


@pytest.mark.parametrize("kind, code, wrong, right", [
    ("kd", 4, key_ev(1, 0x30, False, True), key_ev(1, 0x1E, False, True)),
    ("kd", 4, key_ev(1, 0x1E, False, False), key_ev(1, 0x1E, False, True)),
    ("kd", 4, key_ev(1, 0x1E, False, True, injected=True), key_ev(1, 0x1E, False, True)),
    ("ku", 4, key_ev(1, 0x1E, False, True), key_ev(1, 0x1E, False, False)),
    ("bd", 1, button_ev(1, 2, True, 0, 0), button_ev(1, 1, True, 0, 0)),
    ("bu", 1, button_ev(1, 1, True, 0, 0), button_ev(1, 1, False, 0, 0)),
    ("w", 0, move_ev(1, 0, 0), wheel_ev(1, 0, -120, 0, 0)),
])
def test_echo_check_needs_the_matching_event(kind, code, wrong, right):
    echo = EchoCheck()
    echo.expect(0, kind, code)
    echo.seen(wrong)
    assert echo.overdue(301 * MS)
    echo.seen(right)
    assert not echo.overdue(301 * MS)


def test_echo_check_clears_the_oldest_match_first():
    echo = EchoCheck()
    echo.expect(0, "kd", 4)
    echo.expect(200 * MS, "kd", 4)
    echo.seen(key_ev(210 * MS, 0x1E, False, True))
    assert not echo.overdue(350 * MS)
    assert echo.overdue(501 * MS)


def test_echo_check_is_overdue_only_after_300_ms():
    echo = EchoCheck()
    echo.expect(0, "w")
    assert not echo.overdue(300 * MS)
    assert echo.overdue(300 * MS + 1)
    echo.clear()
    assert not echo.overdue(10_000 * MS)

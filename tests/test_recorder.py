"""Recorder: hook events to steps, the chord cuts and the screenshot bookkeeping
(PLAN.md section 6). GetAsyncKeyState and the screenshot grab are stubs."""
import pytest

from picomp.db import Step
from picomp.hooks import button_ev, hotkey_ev, key_ev, move_ev, wheel_ev
from picomp.recorder import DISCARD_ELSEWHERE, Recorder, ShotWorker, final_shots

MS = 1_000_000
T0 = 50_000 * MS
DISPLAY = (1920, 1080, 96, ((0, 0, 1920, 1080),))  # main screen w, h, dpi, every screen's rect
# This laptop: the main screen, the laptop panel to its left, a 1440p screen beyond that.
THREE_SCREENS = (1920, 1080, 96, ((-4480, 5, -1920, 1445), (-1920, 5, 0, 1085), (0, 0, 1920, 1080)))

# (scan, ext, vk) as the keyboard hook reports them
A = (0x1E, False, 0x41)  # usage 4
B = (0x30, False, 0x42)  # usage 5
C = (0x2E, False, 0x43)  # usage 6
LCTRL = (0x1D, False, 0xA2)  # usage 224
LALT = (0x38, False, 0xA4)  # usage 226
LSHIFT = (0x2A, False, 0xA0)  # usage 225
RSHIFT = (0x36, True, 0xA1)  # usage 229: the layout makes Right Shift extended
RIGHT = (0x4D, True, 0x27)  # usage 0x4F
FAKE_LSHIFT = (0x2A, True, 0xFF)
FAKE_RSHIFT = (0x36, True, 0xFF)  # same scan code as Right Shift, but no key's vk
SIM_RSHIFT = (0x236, True, 0xA1)  # Windows lifting Right Shift around a keypad key
KP4_AS_LEFT = (0x4B, False, 0x25)  # keypad 4 with Shift held and NumLock on: VK_LEFT
SYSRQ = (0x54, False, 0x2C)  # PrintScreen while Alt is down
VOLUME_UP = (0x30, True, 0xAF)  # not in the keymap


class AsyncKeys:
    # GetAsyncKeyState stub: the virtual keys Windows reports as down.
    def __init__(self, *down):
        self.down = set(down)

    def __call__(self, vk):
        return vk in self.down


def button_vk(code):
    return 0x100 + code


def at(ms):
    return T0 + ms * MS


def kd(ms, key):
    return key_ev(at(ms), key[0], key[1], True, vk=key[2])


def ku(ms, key):
    return key_ev(at(ms), key[0], key[1], False, vk=key[2])


def bd(ms, code, x, y):
    return button_ev(at(ms), code, True, x, y)


def bu(ms, code, x, y):
    return button_ev(at(ms), code, False, x, y)


def mv(ms, x, y):
    return move_ev(at(ms), x, y)


def recorder(mode="none", shots=None, keys=None, display=DISPLAY):
    request = shots.append if shots is not None else None
    return Recorder(T0, display, mode, request_shot=request, key_down=keys or AsyncKeys(),
                    button_vk=button_vk)


def feed(rec, *events):
    for ev in events:
        rec.feed(ev)


def stop_chord(rec, ms):
    # Ctrl+Alt+R: the hooks pass Ctrl and Alt on and turn the R into a hotkey.
    feed(rec, kd(ms, LCTRL), kd(ms + 10, LALT), hotkey_ev(at(ms + 20), "record"))


def brief(steps):
    out = []
    for i, s in enumerate(steps):
        assert s.seq == i
        if s.kind == "move":
            out.append((s.t_ms, "move", s.x, s.y))
        elif s.kind in ("bdown", "bup"):
            out.append((s.t_ms, s.kind, s.code, s.x, s.y))
        elif s.kind == "wheel":
            out.append((s.t_ms, "wheel", s.wheel_v, s.wheel_h, s.x, s.y))
        else:
            out.append((s.t_ms, s.kind, s.code))
    return out


def test_moves_are_thinned_to_60_hz_and_keep_the_last_position():
    rec = recorder()
    feed(rec, mv(1, 10, 10), mv(5, 20, 20), mv(10, 30, 30), mv(16, 40, 40), mv(17, 50, 50),
         mv(20, 60, 60), bd(25, 1, 60, 60), bu(30, 1, 60, 60))
    stop_chord(rec, 100)
    assert brief(rec.finish().steps) == [
        (1, "move", 10, 10), (17, "move", 50, 50), (20, "move", 60, 60),
        (25, "bdown", 1, 60, 60), (30, "bup", 1, 60, 60)]


def test_points_are_clamped_to_the_screen():
    rec = recorder()
    feed(rec, mv(1, -5, 2000), bd(10, 1, 5000, -1), bu(20, 1, 5000, -1),
         wheel_ev(at(30), 120, 0, -3, -3))
    stop_chord(rec, 100)
    assert brief(rec.finish().steps) == [
        (1, "move", 0, 1079), (10, "bdown", 1, 1919, 0), (20, "bup", 1, 1919, 0),
        (30, "wheel", 1, 0, 0, 0)]


def test_points_past_the_outer_edge_still_count_as_the_main_screen():
    # Outside every screen (a move pushed past the top edge): clamped, as with one screen.
    rec = recorder(display=THREE_SCREENS)
    feed(rec, mv(1, 100, -3), bd(10, 1, 100, -3), bu(20, 1, 100, -3))
    stop_chord(rec, 100)
    res = rec.finish()
    assert res.discard_reason is None
    assert brief(res.steps) == [(1, "move", 100, 0), (10, "bdown", 1, 100, 0), (20, "bup", 1, 100, 0)]


def test_moves_over_another_screen_are_left_out():
    rec = recorder(display=THREE_SCREENS)
    feed(rec, mv(1, 100, 100), mv(20, -10, 500), mv(40, -3000, 600), mv(60, 200, 300),
         bd(70, 1, 200, 300), bu(80, 1, 200, 300))
    stop_chord(rec, 500)
    res = rec.finish()
    assert res.discard_reason is None
    assert brief(res.steps) == [(1, "move", 100, 100), (60, "move", 200, 300),
                                (70, "bdown", 1, 200, 300), (80, "bup", 1, 200, 300)]


def test_points_past_another_screens_edge_stay_on_that_screen():
    # Pushed past the laptop panel's bottom and top edges and the 1440p screen's left edge:
    # outside every screen, but the pointer is on a side screen, so nothing is recorded.
    rec = recorder(display=THREE_SCREENS)
    feed(rec, mv(1, 100, 500), mv(20, -800, 900), mv(40, -800, 1090), mv(60, -800, 2), mv(80, -4486, 600),
         mv(5000, 300, 300), bd(5010, 1, 300, 300), bu(5020, 1, 300, 300))
    stop_chord(rec, 6000)
    res = rec.finish()
    assert res.discard_reason is None
    assert brief(res.steps) == [(1, "move", 100, 500), (5000, "move", 300, 300),
                                (5010, "bdown", 1, 300, 300), (5020, "bup", 1, 300, 300)]


def test_the_last_point_on_the_main_screen_is_kept_when_the_pointer_leaves():
    rec = recorder(display=THREE_SCREENS)
    feed(rec, mv(100, 300, 500), mv(110, 10, 500), mv(115, -40, 500), mv(5000, 1500, 300),
         kd(5010, A), ku(5020, A))
    stop_chord(rec, 6000)
    assert brief(rec.finish().steps) == [(100, "move", 300, 500), (110, "move", 10, 500),
                                         (5000, "move", 1500, 300), (5010, "kdown", 4), (5020, "kup", 4)]


def test_a_drag_let_go_on_another_screen_before_its_up_arrives_discards_the_recording():
    keys = AsyncKeys(button_vk(1))  # Windows still reports button 1 down
    rec = recorder(display=THREE_SCREENS, keys=keys)
    feed(rec, bd(10, 1, 500, 500), mv(100, 20, 520), mv(120, -500, 600))
    rec.tick(at(400))
    assert rec.discard_reason is None  # still held
    keys.down.clear()  # let go on the laptop panel; the hook's up is still queued
    rec.tick(at(500))
    assert rec.discard_reason == DISCARD_ELSEWHERE


@pytest.mark.parametrize("events", [
    [bd(10, 1, -500, 500), bu(20, 1, -500, 500)],
    [bd(10, 1, 100, 100), mv(15, -500, 500), bu(20, 1, -500, 500)],
    [bd(10, 2, -3000, 900)],
    [wheel_ev(at(10), -120, 0, -500, 500)],
    [mv(5, -800, 900), wheel_ev(at(10), -120, 0, -800, 1090)],
], ids=["click", "drag-out", "press-on-the-1440p-screen", "scroll", "scroll-past-the-panel-edge"])
def test_clicks_and_scrolls_on_another_screen_discard_the_recording(events):
    rec = recorder(display=THREE_SCREENS)
    feed(rec, *events)
    stop_chord(rec, 500)
    assert rec.finish().discard_reason == DISCARD_ELSEWHERE


def test_a_button_up_there_without_a_recorded_press_is_just_dropped():
    rec = recorder(display=THREE_SCREENS)
    feed(rec, bu(10, 1, -500, 500), kd(20, A), ku(30, A))  # pressed before the recording started
    stop_chord(rec, 500)
    res = rec.finish()
    assert res.discard_reason is None
    assert brief(res.steps) == [(20, "kdown", 4), (30, "kup", 4)]


def test_wheel_deltas_accumulate_into_notches():
    rec = recorder()
    for ms, dv, dh in [(1, 60, 0), (2, 60, 0), (3, 90, 0), (4, -200, 0), (5, -20, 0), (6, 10, 0),
                       (7, 0, -120), (8, 120 * 200, 0), (9, -120 * 130, 0)]:
        rec.feed(wheel_ev(at(ms), dv, dh, 100, 200))
    # No key or button down in this recording, so the stop cut keeps everything.
    assert brief(rec.finish().steps) == [
        (2, "wheel", 1, 0, 100, 200),
        (5, "wheel", -1, 0, 100, 200),  # 90 - 200 - 20 = -130: one notch, -10 carried
        (7, "wheel", 0, -1, 100, 200),
        (8, "wheel", 127, 0, 100, 200), (8, "wheel", 73, 0, 100, 200),
        (9, "wheel", -127, 0, 100, 200), (9, "wheel", -3, 0, 100, 200)]


def test_start_chord_ups_repeats_and_orphans_are_dropped():
    rec = recorder()
    feed(rec,
         kd(-50, LCTRL), kd(-40, LALT),  # the start chord, before t0
         kd(0, B),  # at t0 is not after t0
         hotkey_ev(at(0), "record"),
         ku(20, LCTRL), ku(25, LALT), ku(30, B),
         kd(100, A), kd(130, A), kd(160, A), ku(200, A), ku(210, A),
         bu(300, 1, 5, 5),  # held since before t0
         bd(310, 1, 5, 5), bd(320, 1, 5, 5), bu(330, 1, 5, 5))
    stop_chord(rec, 500)
    res = rec.finish()
    assert brief(res.steps) == [(100, "kdown", 4), (200, "kup", 4),
                                (310, "bdown", 1, 5, 5), (330, "bup", 1, 5, 5)]
    assert (res.injected, res.unmapped) == (0, 0)


def test_injected_events_are_counted_and_dropped():
    rec = recorder()
    feed(rec,
         move_ev(at(-1), 1, 1, injected=True),  # before t0: not counted
         move_ev(at(10), 5, 5, injected=True),
         button_ev(at(20), 1, True, 5, 5, injected=True),
         wheel_ev(at(30), 120, 0, 5, 5, injected=True),
         key_ev(at(40), 0x1E, False, True, vk=0x41, injected=True),
         key_ev(at(50), 0x1E, False, False, vk=0x41, injected=True),
         kd(100, B), ku(150, B))
    stop_chord(rec, 500)
    res = rec.finish()
    assert res.injected == 5
    assert brief(res.steps) == [(100, "kdown", 5), (150, "kup", 5)]


def test_fake_shifts_and_unmapped_keys_are_dropped():
    rec = recorder()
    feed(rec,
         kd(10, FAKE_LSHIFT), kd(11, RIGHT), ku(60, RIGHT), ku(61, FAKE_LSHIFT),
         kd(70, FAKE_RSHIFT), ku(71, FAKE_RSHIFT),
         kd(100, VOLUME_UP), ku(150, VOLUME_UP),
         kd(200, LSHIFT), ku(250, LSHIFT))
    stop_chord(rec, 500)
    res = rec.finish()
    assert brief(res.steps) == [(11, "kdown", 0x4F), (60, "kup", 0x4F),
                                (200, "kdown", 225), (250, "kup", 225)]
    assert res.unmapped == 2


def test_right_shift_is_recorded():
    rec = recorder()
    feed(rec, kd(10, RSHIFT), kd(20, A), ku(30, A), ku(40, RSHIFT))
    stop_chord(rec, 500)
    assert brief(rec.finish().steps) == [(10, "kdown", 229), (20, "kdown", 4), (30, "kup", 4),
                                         (40, "kup", 229)]


def test_the_shift_windows_lifts_around_a_keypad_key_is_dropped():
    # Hardware log, NumLock on: Right Shift held, keypad 4 tapped. The Shift that
    # Windows presses again carries the plain scan code: auto-repeat here.
    rec = recorder()
    feed(rec, kd(10, RSHIFT), ku(20, SIM_RSHIFT), kd(20, KP4_AS_LEFT), ku(60, KP4_AS_LEFT), kd(60, RSHIFT),
         ku(100, RSHIFT))
    stop_chord(rec, 500)
    res = rec.finish()
    assert brief(res.steps) == [(10, "kdown", 229), (20, "kdown", 0x5C), (60, "kup", 0x5C), (100, "kup", 229)]
    assert res.unmapped == 0


def test_alt_printscreen_records_printscreen():
    rec = recorder()
    feed(rec, kd(10, LALT), kd(20, SYSRQ), ku(30, SYSRQ), ku(40, LALT))
    stop_chord(rec, 500)
    res = rec.finish()
    assert brief(res.steps) == [(10, "kdown", 226), (20, "kdown", 0x46), (30, "kup", 0x46), (40, "kup", 226)]
    assert res.unmapped == 0


def test_fake_right_shift_around_a_gray_key_keeps_right_shift_held():
    # A PS/2 keyboard with NumLock off: Right Shift held, Right pressed.
    rec = recorder()
    feed(rec, kd(10, RSHIFT), ku(20, FAKE_RSHIFT), kd(20, RIGHT), ku(60, RIGHT), kd(60, FAKE_RSHIFT),
         ku(100, RSHIFT))
    stop_chord(rec, 500)
    assert brief(rec.finish().steps) == [(10, "kdown", 229), (20, "kdown", 0x4F), (60, "kup", 0x4F),
                                         (100, "kup", 229)]


def test_missed_ups_are_inserted_on_the_tick():
    keys = AsyncKeys(A[2])
    rec = recorder(keys=keys)
    rec.feed(kd(100, A))
    rec.tick(at(200))  # held 100 ms: too young to check
    rec.tick(at(400))  # Windows still reports it down
    keys.down.clear()  # let go while a UAC prompt had the input
    rec.tick(at(650))
    rec.feed(ku(700, A))  # the hook's own up, if it ever comes, is an orphan now
    keys.down.add(button_vk(1))
    feed(rec, bd(1000, 1, 7, 8), mv(1100, 50, 60), mv(1105, 55, 65))
    rec.tick(at(1200))
    keys.down.clear()
    rec.tick(at(1300))
    stop_chord(rec, 2000)
    assert brief(rec.finish().steps) == [
        (100, "kdown", 4), (650, "kup", 4), (1000, "bdown", 1, 7, 8),
        (1100, "move", 50, 60), (1105, "move", 55, 65),  # the pending move goes first
        (1300, "bup", 1, 55, 65)]  # at the last position seen


def test_stop_cut_removes_the_chord_and_trailing_moves():
    rec = recorder()
    feed(rec, kd(100, A), ku(150, A), mv(300, 10, 10), mv(400, 20, 20),
         kd(500, LCTRL), mv(520, 30, 30), kd(540, LALT), hotkey_ev(at(560), "record"),
         mv(600, 40, 40))
    assert rec.finish().steps == [Step(0, 100, "kdown", None, None, 4, None, None),
                                  Step(1, 150, "kup", None, None, 4, None, None)]


def test_stop_cut_goes_back_to_the_press_of_anything_still_held():
    shots = []
    rec = recorder("each_click", shots)
    feed(rec, kd(100, A), ku(150, A), bd(200, 1, 10, 10), mv(300, 20, 20))
    stop_chord(rec, 400)  # the button is still down
    res = rec.finish()
    assert brief(res.steps) == [(100, "kdown", 4), (150, "kup", 4)]
    assert shots == [2]
    assert final_shots("each_click", {2: b"png"}, len(res.steps)) == {}


def test_shutdown_cut_removes_the_ctrl_c():
    rec = recorder()
    feed(rec, kd(100, A), ku(150, A), mv(200, 30, 30), kd(300, LCTRL), kd(320, C))
    assert brief(rec.finish().steps) == [(100, "kdown", 4), (150, "kup", 4)]


def test_a_recording_of_only_moves_ends_up_empty():
    rec = recorder()
    feed(rec, mv(10, 1, 1), mv(50, 2, 2), mv(90, 3, 3))
    assert rec.step_count == 3
    assert rec.finish().steps == []


def test_t_ms_is_floored_and_never_decreases():
    rec = recorder()
    feed(rec, move_ev(T0 + 1, 3, 3), key_ev(T0 + 1_999_999, 0x1E, False, True, vk=0x41), ku(5, A),
         kd(100, B),
         bd(95, 1, 5, 5),  # the mouse hook's stamp is older, but it was queued later
         bu(120, 1, 5, 5), ku(110, B))
    stop_chord(rec, 500)
    assert brief(rec.finish().steps) == [
        (0, "move", 3, 3), (1, "kdown", 4), (5, "kup", 4),
        (100, "kdown", 5), (100, "bdown", 1, 5, 5), (120, "bup", 1, 5, 5), (120, "kup", 5)]


def test_each_click_requests_a_shot_per_recorded_press():
    shots = []
    rec = recorder("each_click", shots)
    feed(rec, mv(10, 1, 1), mv(12, 2, 2),
         bd(20, 1, 2, 2), bd(25, 1, 2, 2), bu(30, 1, 2, 2),
         bd(40, 2, 3, 3), bu(50, 2, 3, 3))
    stop_chord(rec, 100)
    res = rec.finish()
    assert shots == [2, 4]
    assert len(res.steps) == 6
    assert [res.steps[i].kind for i in shots] == ["bdown", "bdown"]
    kept = final_shots("each_click", {2: b"a", 4: b"b", 6: b"c", "end": b"z"}, len(res.steps))
    assert kept == {2: b"a", 4: b"b"}


def test_start_end_shots_are_stored_at_seq_0_and_n():
    shots = []
    rec = recorder("start_end", shots)
    assert shots == [0]
    feed(rec, bd(100, 1, 5, 5), bu(150, 1, 5, 5))
    stop_chord(rec, 300)
    res = rec.finish()
    assert shots == [0, "end"]
    n = len(res.steps)
    assert n == 2
    assert final_shots("start_end", {0: b"start", "end": b"stop"}, n) == {0: b"start", 2: b"stop"}
    assert final_shots("start_end", {"end": b"stop"}, n) == {2: b"stop"}


def test_no_shots_mode():
    shots = []
    rec = recorder("none", shots)
    feed(rec, bd(100, 1, 5, 5), bu(150, 1, 5, 5))
    rec.finish()
    assert shots == []
    assert final_shots("none", {0: b"x", "end": b"y"}, 2) == {}


def test_the_first_discard_reason_wins():
    rec = recorder()
    rec.discard("display changed")
    rec.discard("something else")
    feed(rec, kd(100, A), ku(150, A))
    stop_chord(rec, 300)
    res = rec.finish()
    assert rec.discard_reason == "display changed"
    assert res.discard_reason == "display changed"
    assert brief(res.steps) == [(100, "kdown", 4), (150, "kup", 4)]
    assert (rec.display, rec.shots_mode) == (DISPLAY, "none")


def test_shot_worker_keeps_only_the_current_generation():
    grabs = []

    def grab():
        grabs.append(1)
        return b"png%d" % len(grabs)

    w = ShotWorker(grab)
    w.begin()
    w.request(1)  # from an earlier recording: never grabbed
    w.begin()
    w.request(0)
    w.request("end")
    w.start()
    try:
        got = w.drain(5.0)
    finally:
        w.stop()
    assert got == {0: b"png1", "end": b"png2"}
    assert len(grabs) == 2


def test_shot_worker_skips_failed_grabs():
    results = [RuntimeError("secure desktop"), None, b"ok"]

    def grab():
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    w = ShotWorker(grab)
    w.stop()  # never started: nothing to stop
    w.begin()
    for k in (0, 1, 2):
        w.request(k)
    w.start()
    try:
        assert w.drain(5.0) == {2: b"ok"}
    finally:
        w.stop()
    w.stop()

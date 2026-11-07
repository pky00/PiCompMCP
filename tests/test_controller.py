"""Controller: hotkey routing by state, board events, the PING liveness gate, the
display checks and the replay start refusals. Built with Controller(db_path=tmp)
and no startup(), so no hook, thread or port is started; win.display, the hooks,
the board and the player are stubs."""
import time
from types import SimpleNamespace

import pytest

from picomp import player, win
from picomp.controller import (DISCARD_DISPLAY, REASON_DISPLAY, REASON_HALT, REASON_KILLED, REASON_TIMEOUT,
                               STATES, WARN_DISPLAY, WARN_ELSEWHERE, Controller, NotFound, Refused)
from picomp.db import Step
from picomp.hooks import button_ev, hotkey_ev, key_ev, move_ev
from picomp.recorder import DISCARD_ELSEWHERE

MS = 1_000_000
T0 = 10_000 * MS
DISPLAY = (1920, 1080, 96, ((0, 0, 1920, 1080),))  # main screen w, h, dpi, every screen's rect
# This laptop: the main screen, the laptop panel to its left, a 1440p screen beyond that.
THREE_SCREENS = (1920, 1080, 96, ((-4480, 5, -1920, 1445), (-1920, 5, 0, 1085), (0, 0, 1920, 1080)))

# (scan, vk) as the keyboard hook reports them
A = (0x1E, 0x41)
LCTRL = (0x1D, 0xA2)
LALT = (0x38, 0xA4)

# hotkey -> {state: the call it makes}; every other state ignores it
HOTKEY_ACTIONS = {
    "record": {"idle": ("start", T0), "recording": ("stop", T0)},
    "kill": {"replaying": ("kill",), "paused": ("kill",)},
    "shutdown": {"recording": ("stop", T0)},
}

# kind, line, the stop reason, how the player stops
BOARD_EVENTS = [
    ("halt", "!HALT", REASON_HALT, "released"),
    ("timeout", "!TIMEOUT", REASON_TIMEOUT, "released"),
    ("lost", "", player.REASON_LOST, "lost"),
    ("err", "ERR KD usb-busy", "board error: ERR KD usb-busy", "normal"),
]


class FakeHooks:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1


class FakeBoard:
    status, detail = "connected", "COM7, firmware 0.1.0"

    def __init__(self, online=True, answers=True):
        self.online = online
        self.answers = answers
        self.on_hello = None
        self.sent = []

    def hello(self):
        if self.on_hello:
            self.on_hello()
        return self.answers

    def send(self, line):
        self.sent.append(line)
        return True


class FakePlayer:
    def __init__(self, phase):
        self.phase = phase
        self.step = 0
        self.total = 3
        self.alive_ns = time.perf_counter_ns()
        self.stops = []
        self.pauses = []
        self.observed = []

    def stop(self, reason, how="normal"):
        self.stops.append((reason, how))
        self.phase = "done"

    def pause(self, reason):
        self.pauses.append(reason)

    def observe(self, ev):
        self.observed.append(ev)


@pytest.fixture
def display(monkeypatch):
    # current[0] is what win.display() returns; tests change it.
    current = [DISPLAY]
    monkeypatch.setattr(win, "display", lambda: current[0])
    return current


@pytest.fixture
def ctl(tmp_path, display):
    c = Controller(db_path=tmp_path / "picomp.db")
    c.hooks = FakeHooks()
    c.board = FakeBoard()
    return c


def key(ms, k, down):
    return key_ev(T0 + ms * MS, k[0], False, down, vk=k[1])


def replaying(c, state="replaying", phase="running"):
    p = FakePlayer(phase)
    c._state = state
    c._player = p
    c._replay = {"id": 1, "name": "Login", "step": 0, "total": p.total}
    c._replay_display = DISPLAY
    return p


def record_calls(c):
    calls = []
    c.start_recording = lambda t_ns: calls.append(("start", t_ns))
    c.stop_recording = lambda t_ns: calls.append(("stop", t_ns))
    c.kill = lambda: calls.append(("kill",))
    return calls


def save_recording(c, steps=(Step(0, 0, "move", 5, 5),)):
    c.db.init()
    meta = {"name": "Login", "created_at": "2026-10-21T11:02:11Z", "screen_w": 1920, "screen_h": 1080,
            "dpi": 96, "duration_ms": 0, "shots_mode": "none"}
    return c.db.save_recording(meta, list(steps), {})


# --- hotkeys ---

@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("name", sorted(HOTKEY_ACTIONS))
def test_hotkeys_act_only_in_their_states(ctl, name, state):
    calls = record_calls(ctl)
    ctl._state = state
    ctl.handle_event(hotkey_ev(T0, name))
    action = HOTKEY_ACTIONS[name].get(state)
    assert calls == ([action] if action else [])


def test_input_events_reach_the_held_model_the_recorder_and_the_player(ctl):
    fed = []
    ctl._recorder = SimpleNamespace(feed=fed.append, discard_reason=None)
    p = replaying(ctl)
    ev = key(100, A, True)
    ctl.handle_event(ev)
    ctl.handle_event(hotkey_ev(T0, "record"))  # ignored while replaying, and never fed on
    assert fed == [ev]
    assert p.observed == [ev]
    assert ctl.held.snapshot() == ({(A[0], False): A[1]}, set())


def test_record_hotkey_starts_and_stops_a_real_recording(ctl):
    ctl.db.init()
    ctl.handle_event(hotkey_ev(T0, "record"))
    assert ctl.status()["state"] == "recording"
    # A typed, then the stop chord: its Ctrl and Alt reach the recorder, its R is the hotkey.
    for ev in (key(100, A, True), key(150, A, False), key(500, LCTRL, True), key(510, LALT, True)):
        ctl.handle_event(ev)
    assert ctl.status()["recording"] == {"steps": 4}
    ctl.handle_event(hotkey_ev(T0 + 520 * MS, "record"))
    st = ctl.status()
    assert (st["state"], st["recording"]) == ("idle", None)
    assert st["message"].startswith('Saved "Recording ')
    assert st["message"].endswith('": 2 steps, 0 shots, 0 software-injected inputs ignored')
    [rec] = ctl.list_recordings()
    assert (rec["steps"], rec["duration_ms"], rec["screen_w"], rec["screen_h"], rec["dpi"]) == (
        2, 150, 1920, 1080, 96)
    assert [s.kind for s in ctl.db.load_steps(rec["id"])] == ["kdown", "kup"]


def test_recording_works_with_other_screens_connected(ctl, display):
    ctl.db.init()
    ctl._settings["screenshots"] = "none"  # no screenshot worker runs here
    display[0] = THREE_SCREENS
    ctl.handle_event(hotkey_ev(T0, "record"))
    assert ctl.status()["state"] == "recording"
    for ev in (button_ev(T0 + 100 * MS, 1, True, 500, 500), button_ev(T0 + 150 * MS, 1, False, 500, 500),
               key(500, LCTRL, True), key(510, LALT, True)):
        ctl.handle_event(ev)
    ctl.handle_event(hotkey_ev(T0 + 520 * MS, "record"))
    st = ctl.status()
    assert st["message"].startswith('Saved "Recording ')
    [rec] = ctl.list_recordings()
    assert (rec["steps"], rec["screen_w"], rec["screen_h"], rec["dpi"]) == (2, 1920, 1080, 96)


def test_a_click_on_another_screen_warns_then_discards_the_recording(ctl, display):
    display[0] = THREE_SCREENS
    ctl.handle_event(hotkey_ev(T0, "record"))
    ctl.handle_event(button_ev(T0 + 100 * MS, 1, True, -500, 500))  # on the laptop panel
    assert ctl.status()["message"] == WARN_ELSEWHERE
    ctl.handle_event(button_ev(T0 + 150 * MS, 1, False, -500, 500))
    ctl.handle_event(hotkey_ev(T0 + 400 * MS, "record"))
    st = ctl.status()
    assert (st["state"], st["message"]) == ("idle", "Recording discarded: " + DISCARD_ELSEWHERE)


def test_a_drag_let_go_on_another_screen_warns_on_the_tick(ctl, display, monkeypatch):
    monkeypatch.setattr(win, "key_down", lambda vk: False)  # Windows already reports the button up
    ctl._settings["screenshots"] = "none"
    display[0] = THREE_SCREENS
    ctl.handle_event(hotkey_ev(T0, "record"))
    ctl.handle_event(button_ev(T0 + 100 * MS, 1, True, 500, 500))
    ctl.handle_event(move_ev(T0 + 200 * MS, -500, 600))  # dragged onto the laptop panel
    assert ctl.status()["message"] == ""
    ctl.tick(T0 + 500 * MS)  # the up is still queued behind the tick
    assert ctl.status()["message"] == WARN_ELSEWHERE


# --- display checks (the 250 ms tick) ---

def test_display_change_during_a_recording_warns_then_discards_it(ctl, display):
    ctl.handle_event(hotkey_ev(T0, "record"))
    ctl.handle_event(key(100, A, True))
    ctl.handle_event(key(150, A, False))
    display[0] = (1920, 1080, 120, DISPLAY[3])  # scaling changed to 125 %
    ctl.tick(T0 + 300 * MS)
    assert ctl.status()["message"] == WARN_DISPLAY
    ctl.handle_event(hotkey_ev(T0 + 400 * MS, "record"))
    st = ctl.status()
    assert (st["state"], st["message"]) == ("idle", "Recording discarded: " + DISCARD_DISPLAY)


@pytest.mark.parametrize("phase, pauses", [("running", [REASON_DISPLAY]), ("paused", [])])
def test_display_change_pauses_a_running_replay(ctl, display, phase, pauses):
    p = replaying(ctl, phase=phase)
    ctl.tick(T0)
    assert p.pauses == []
    display[0] = THREE_SCREENS  # two more screens plugged in
    ctl.tick(T0 + 250 * MS)
    assert p.pauses == pauses


# --- board events (board reader thread) ---

@pytest.mark.parametrize("state, phase", [("replaying", "running"), ("paused", "paused")])
@pytest.mark.parametrize("kind, line, reason, how", BOARD_EVENTS)
def test_board_events_stop_the_replay(ctl, state, phase, kind, line, reason, how):
    p = replaying(ctl, state, phase)
    ctl.on_board_event(kind, line)
    assert p.stops == [(reason, how)]
    st = ctl.status()
    assert (st["state"], st["replay"], st["message"]) == ("idle", None, reason)


@pytest.mark.parametrize("kind, line, reason, how", BOARD_EVENTS)
def test_board_events_while_idle_only_set_the_message(ctl, kind, line, reason, how):
    ctl.on_board_event(kind, line)
    st = ctl.status()
    assert (st["state"], st["message"]) == ("idle", reason)


def test_errors_from_commands_in_flight_keep_the_halt_message(ctl):
    p = replaying(ctl)
    ctl.on_board_event("halt", "!HALT")
    ctl.on_board_event("err", "ERR M halted")
    assert p.stops == [(REASON_HALT, "released")]
    assert ctl.status()["message"] == REASON_HALT


def test_reconnecting_clears_only_the_disconnect_message(ctl):
    ctl.on_board_event("lost", "")
    ctl.on_board_event("connected", "")
    assert ctl.status()["message"] == ""
    ctl.on_board_event("timeout", "!TIMEOUT")
    ctl.on_board_event("connected", "")
    assert ctl.status()["message"] == REASON_TIMEOUT


# --- PING liveness gate ---

@pytest.mark.parametrize("phase", [None, "ready", "done"])
def test_ping_ok_without_an_active_replay(ctl, phase):
    if phase:
        replaying(ctl, phase=phase)
    ctl._dispatch_alive_ns = 0  # a stale stamp only matters while a replay is active
    assert ctl.ping_ok()


@pytest.mark.parametrize("phase", ["running", "paused", "resuming"])
def test_ping_ok_needs_fresh_liveness_stamps_during_a_replay(ctl, phase):
    p = replaying(ctl, phase=phase)
    now = time.perf_counter_ns()
    ctl._dispatch_alive_ns = p.alive_ns = now
    assert ctl.ping_ok()
    ctl._dispatch_alive_ns = now - 2_000 * MS  # the dispatcher hung
    assert not ctl.ping_ok()
    ctl._dispatch_alive_ns, p.alive_ns = now, now - 2_000 * MS  # the replay thread hung
    assert not ctl.ping_ok()


# --- replay start, resume and stop ---

@pytest.mark.parametrize("board, screen, state, reason", [
    ({"online": False}, DISPLAY, "idle", "board is not connected"),
    ({}, (2560, 1440, 144, ((0, 0, 2560, 1440),)), "idle",
     "screen is 2560x1440 at 144 dpi but the recording was made at 1920x1080 at 96 dpi"),
    ({"answers": False}, DISPLAY, "idle", "board did not answer HELLO"),
    ({}, DISPLAY, "recording", "cannot replay while recording"),
], ids=["offline", "other-screen", "no-hello", "recording"])
def test_replay_refusals(ctl, display, board, screen, state, reason):
    rec_id = save_recording(ctl)
    ctl.board = FakeBoard(**board)
    display[0] = screen
    ctl._state = state
    with pytest.raises(Refused) as e:
        ctl.start_replay(rec_id)
    assert str(e.value) == reason
    st = ctl.status()
    assert (st["state"], st["replay"], st["message"]) == (state, None, reason)
    assert ctl._player is None
    assert ctl.board.sent == []


def test_replay_starts_with_other_screens_connected(ctl, display):
    rec_id = save_recording(ctl)
    display[0] = THREE_SCREENS
    launched = []
    ctl._launch_player = lambda rid, screen: launched.append((rid, screen)) or SimpleNamespace(total=1)
    assert ctl.start_replay(rec_id) == {"id": rec_id, "total": 1}
    assert launched == [(rec_id, (1920, 1080))]  # the player drives the main screen
    assert ctl.status()["state"] == "replaying"
    assert ctl._replay_display == THREE_SCREENS  # a change to any screen pauses it


def test_replay_of_a_recording_without_steps_is_refused(ctl):
    rec_id = save_recording(ctl, steps=())
    with pytest.raises(Refused) as e:
        ctl.start_replay(rec_id)
    assert str(e.value) == "recording has no steps"
    assert ctl.status()["state"] == "idle"


def test_replay_of_a_missing_recording_is_not_found(ctl):
    ctl.db.init()
    with pytest.raises(NotFound):
        ctl.start_replay(99)
    assert ctl.status()["state"] == "idle"


def test_a_kill_while_the_replay_is_starting_cancels_it(ctl):
    rec_id = save_recording(ctl)
    # Ctrl+Alt+Esc while start_replay waits for the HELLO reply, before any player exists.
    ctl.board.on_hello = lambda: ctl.handle_event(hotkey_ev(T0, "kill"))
    with pytest.raises(Refused) as e:
        ctl.start_replay(rec_id)
    assert str(e.value) == REASON_KILLED
    st = ctl.status()
    assert (st["state"], st["replay"], st["message"]) == ("idle", None, REASON_KILLED)
    assert ctl._player is None
    assert ctl.hooks.restarts == 1
    assert ctl.board.sent == []  # no step went out


def test_stop_without_a_replay_does_nothing(ctl):
    ctl.stop_replay()
    assert ctl._start_cancel is None  # only a replay that is starting gets cancelled
    st = ctl.status()
    assert (st["state"], st["message"]) == ("idle", "")


def test_resume_refusals(ctl, display):
    with pytest.raises(Refused) as e:
        ctl.resume_replay()
    assert str(e.value) == "nothing to resume"
    replaying(ctl, "paused", "paused")
    display[0] = (1920, 1080, 120, DISPLAY[3])
    with pytest.raises(Refused) as e:
        ctl.resume_replay()
    assert str(e.value) == "cannot resume: " + REASON_DISPLAY
    assert ctl.status()["state"] == "paused"
    assert ctl.hooks.restarts == 0

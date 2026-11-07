"""TouchDetector: which hook events are the board's own and which mean you
touched the mouse or keyboard (PLAN.md section 7, touch detection)."""
import pytest

from picomp.hooks import Ev, button_ev, hotkey_ev, key_ev, move_ev, wheel_ev
from picomp.player import TouchDetector

MS = 1_000_000
T = 10_000 * MS  # the mark: t_start or t_resume

A = (0x1E, False)  # usage 4
B = (0x30, False)  # usage 5
ENTER = (0x1C, False)
LCTRL = (0x1D, False)  # usage 224
RSHIFT = (0x36, True)  # usage 229, with vk VK_RSHIFT
FAKE_LSHIFT = (0x2A, True)
FAKE_RSHIFT = (0x36, True)  # with a vk that is not VK_RSHIFT
VOLUME_UP = (0x30, True)  # not in the keymap


def at(ms):
    return T + ms * MS


def key(ms, sk, down, injected=False, vk=0):
    return key_ev(at(ms), sk[0], sk[1], down, vk=vk, injected=injected)


def detector(held_keys=(), held_buttons=(), cursor=(100, 100)):
    d = TouchDetector()
    d.mark(T, held_keys, held_buttons, cursor)
    return d


def test_nothing_is_a_touch_before_the_first_mark():
    d = TouchDetector()
    assert not d.is_touch(key(10, B, True))
    assert not d.is_touch(move_ev(at(10), 3000, 3000))


def test_hotkeys_and_injected_events_are_never_a_touch():
    d = detector()
    assert not d.is_touch(hotkey_ev(at(10), "kill"))
    assert not d.is_touch(key(10, B, True, injected=True))
    assert not d.is_touch(move_ev(at(10), 3000, 3000, injected=True))
    assert not d.is_touch(button_ev(at(10), 1, True, 0, 0, injected=True))
    assert not d.is_touch(wheel_ev(at(10), 120, 0, 0, 0, injected=True))


def test_move_within_10_px_of_the_cursor_at_the_mark():
    d = detector(cursor=(100, 100))
    assert not d.is_touch(move_ev(at(10), 110, 90))
    assert d.is_touch(move_ev(at(20), 111, 100))
    assert d.is_touch(move_ev(at(30), 100, 89))


def test_move_with_no_reference_position_is_a_touch():
    d = detector(cursor=None)
    assert d.is_touch(move_ev(at(10), 0, 0))


def test_move_within_2_px_of_a_position_sent_in_the_last_500_ms():
    d = detector(cursor=(0, 0))
    d.sent_pos(at(10), 500, 500)
    d.sent_pos(at(20), 900, 900)  # the last position sent
    assert not d.is_touch(move_ev(at(30), 502, 498))
    assert d.is_touch(move_ev(at(40), 503, 500))
    assert not d.is_touch(move_ev(at(50), 909, 891))
    assert d.is_touch(move_ev(at(60), 910, 911))
    assert not d.is_touch(move_ev(at(510), 501, 501))  # sent exactly 500 ms ago
    assert d.is_touch(move_ev(at(511), 501, 501))


def test_a_new_mark_forgets_what_was_sent():
    d = detector(cursor=(0, 0))
    d.sent_pos(at(10), 500, 500)
    d.sent_key(at(10), 4, True)
    d.sent_wheel(at(10))
    d.mark(at(100), (), (), (700, 700))
    assert d.is_touch(move_ev(at(110), 500, 500))
    assert not d.is_touch(move_ev(at(120), 705, 700))
    assert d.is_touch(key(130, A, True))
    assert d.is_touch(wheel_ev(at(140), 120, 0, 700, 700))


@pytest.mark.parametrize("sk", [FAKE_LSHIFT, FAKE_RSHIFT])
def test_fake_shifts_are_never_a_touch(sk):
    d = detector()
    assert not d.is_touch(key(10, sk, True, vk=0xFF))
    assert not d.is_touch(key(20, sk, False, vk=0xFF))


def test_shifts_windows_simulates_are_never_a_touch():
    d = detector()
    assert not d.is_touch(key(10, (0x236, True), False, vk=0xA1))
    assert not d.is_touch(key(20, (0x22A, False), True, vk=0xA0))


def test_right_shift_from_the_board_is_not_a_touch_but_yours_is():
    d = detector()
    d.sent_key(at(10), 229, True)
    assert not d.is_touch(key(12, RSHIFT, True, vk=0xA1))
    d.sent_key(at(20), 229, False)
    assert not d.is_touch(key(22, RSHIFT, False, vk=0xA1))
    assert d.is_touch(key(600, RSHIFT, True, vk=0xA1))


def test_auto_repeat_of_a_key_the_replay_holds():
    d = detector()
    d.sent_key(at(10), 4, True)
    assert not d.is_touch(key(2000, A, True))
    d.sent_key(at(2100), 4, False)
    assert d.is_touch(key(3000, A, True))  # released: a new press is yours


def test_a_kd_echo_processed_after_its_ku_was_sent():
    d = detector()
    d.sent_key(at(10), 4, True)
    d.sent_key(at(20), 4, False)
    assert not d.is_touch(key(12, A, True))
    assert not d.is_touch(key(25, A, False))
    assert d.is_touch(key(511, A, True))


def test_a_key_the_replay_did_not_send_is_a_touch():
    d = detector()
    d.sent_key(at(10), 4, True)
    assert d.is_touch(key(20, B, True))
    assert d.is_touch(key(30, B, False))


@pytest.mark.parametrize("down", [True, False])
def test_an_unmapped_key_is_a_touch(down):
    assert detector().is_touch(key(10, VOLUME_UP, down))


def test_a_key_held_at_the_mark():
    d = detector(held_keys=[ENTER])  # the Enter that pressed Replay
    assert not d.is_touch(key(30, ENTER, True))  # its auto-repeat
    assert not d.is_touch(key(600, ENTER, False))  # its up, however late
    assert d.is_touch(key(900, ENTER, True))  # a new press


def test_events_queued_before_the_mark_update_the_held_snapshot():
    d = detector(held_keys=[B])
    assert not d.is_touch(key(-5, A, True))
    assert not d.is_touch(key(-3, B, False))
    assert not d.is_touch(button_ev(at(-2), 1, True, 0, 0))
    assert not d.is_touch(move_ev(at(-1), 3000, 3000))
    assert not d.is_touch(key(40, A, True))
    assert not d.is_touch(key(700, A, False))
    assert not d.is_touch(button_ev(at(800), 1, False, 0, 0))
    assert d.is_touch(key(900, B, True))  # B was let go before the mark


def test_release_all_counts_as_an_up_for_everything_the_replay_held():
    d = detector()
    d.sent_key(at(10), 224, True)
    d.sent_button(at(10), 1, True)
    d.sent_release_all(at(50))
    assert not d.is_touch(key(60, LCTRL, False))
    assert not d.is_touch(button_ev(at(60), 1, False, 0, 0))
    assert d.is_touch(key(2000, LCTRL, True))


def test_a_button_is_the_boards_only_for_the_same_button_and_direction():
    d = detector()
    d.sent_button(at(10), 1, True)
    assert not d.is_touch(button_ev(at(15), 1, True, 0, 0))
    assert d.is_touch(button_ev(at(20), 1, False, 0, 0))
    assert d.is_touch(button_ev(at(20), 2, True, 0, 0))
    d.sent_button(at(30), 1, False)
    assert not d.is_touch(button_ev(at(35), 1, False, 0, 0))
    assert d.is_touch(button_ev(at(700), 1, True, 0, 0))


def test_the_up_of_a_button_held_at_the_mark():
    d = detector(held_buttons=[1])  # the click that pressed Resume
    assert not d.is_touch(button_ev(at(100), 1, False, 0, 0))
    assert d.is_touch(button_ev(at(200), 1, True, 0, 0))
    assert d.is_touch(button_ev(at(300), 1, False, 0, 0))


def test_wheel():
    d = detector()
    assert d.is_touch(wheel_ev(at(10), -120, 0, 0, 0))
    d.sent_wheel(at(20))
    assert not d.is_touch(wheel_ev(at(30), -120, 0, 0, 0))
    assert not d.is_touch(wheel_ev(at(520), 0, 120, 0, 0))
    assert d.is_touch(wheel_ev(at(521), 0, 120, 0, 0))


def test_an_unknown_event_kind_is_a_touch():
    assert detector().is_touch(Ev(at(10), "gesture"))

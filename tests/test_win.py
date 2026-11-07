# picomp/win.py pixel <-> HID mapping. Pure functions only: importing win binds
# ctypes prototypes but calls nothing, and no test here calls Win32.
import pytest

from picomp import win

SIZES = [2, 720, 768, 1080, 1200, 1366, 1440, 1600, 1920, 2160, 2560, 3840]


def test_hid_max():
    assert win.HID_MAX == 32767


def test_screenshots_take_the_main_screen():
    # mss's list on this laptop: all screens, then Windows' order, laptop panel first.
    monitors = [{"left": -4480, "top": 0, "width": 6400, "height": 1445},
                {"left": -1920, "top": 5, "width": 1920, "height": 1080},
                {"left": -4480, "top": 5, "width": 2560, "height": 1440},
                {"left": 0, "top": 0, "width": 1920, "height": 1080}]
    assert win.main_monitor(monitors) is monitors[3]
    assert win.main_monitor(monitors[:2]) is monitors[1]  # no screen at (0, 0): the first one


@pytest.mark.parametrize("size", SIZES)
def test_edges_are_exact(size):
    assert win.px_to_hid(0, size) == 0
    assert win.px_to_hid(size - 1, size) == win.HID_MAX
    assert win.hid_to_px(0, size) == 0
    assert win.hid_to_px(win.HID_MAX, size) == size - 1


@pytest.mark.parametrize("size", SIZES)
def test_off_screen_points_are_clamped(size):
    assert win.px_to_hid(-1, size) == 0
    assert win.px_to_hid(-5000, size) == 0
    assert win.px_to_hid(size, size) == win.HID_MAX
    assert win.px_to_hid(size * 3, size) == win.HID_MAX


@pytest.mark.parametrize("size", SIZES)
def test_round_trip_within_one_pixel(size):
    for p in range(size):
        assert abs(win.hid_to_px(win.px_to_hid(p, size), size) - p) <= 1, p


@pytest.mark.parametrize("size", SIZES)
def test_mapping_is_strictly_increasing_ints_in_range(size):
    prev = -1
    for p in range(size):
        v = win.px_to_hid(p, size)
        assert isinstance(v, int)
        assert 0 <= v <= win.HID_MAX
        assert v > prev, p
        prev = v


def test_wire_centre_is_the_screen_centre():
    # PLAN.md section 4: "M 16384 16384" puts the cursor at the centre.
    assert win.hid_to_px(16384, 1920) == 960
    assert win.hid_to_px(16384, 1080) == 540


@pytest.mark.parametrize("point, expected", [
    ((0, 0), (0, 0)),
    ((960, 540), (960, 540)),
    ((1919, 1079), (1919, 1079)),
    ((-1, -1), (0, 0)),
    ((1920, 1080), (1919, 1079)),
    ((-500, 5000), (0, 1079)),
    ((4000, -3), (1919, 0)),
])
def test_clamp_point(point, expected):
    assert win.clamp_point(point[0], point[1], 1920, 1080) == expected


def test_clamped_off_screen_point_maps_to_the_edge():
    x, y = win.clamp_point(-40, 2000, 1920, 1080)
    assert (win.px_to_hid(x, 1920), win.px_to_hid(y, 1080)) == (0, win.HID_MAX)

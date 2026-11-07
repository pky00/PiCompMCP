"""Win32 calls through ctypes: DPI setup, screen geometry, pixel <-> HID conversion,
cursor and foreground window, key state, the SendInput release fallback and screenshots.

Call setup_dpi() before anything else in the process: without per-monitor DPI
awareness every pixel value here (and in pynput and mss) is scaled.
"""
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

HID_MAX = 32767

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_SWAPBUTTON = 23
MONITOR_DEFAULTTOPRIMARY = 1
MDT_EFFECTIVE_DPI = 0
DPI_AWARENESS_PER_MONITOR_AWARE = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
SW_RESTORE = 9
GA_ROOT = 2
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_ASYNCWINDOWPOS = 0x4000

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XUP = 0x0100
XBUTTON1 = 1
XBUTTON2 = 2

_BUTTON_VK = {1: VK_LBUTTON, 2: VK_RBUTTON, 3: VK_MBUTTON, 4: VK_XBUTTON1, 5: VK_XBUTTON2}
# board button code -> (SendInput flags, mouseData)
_BUTTON_UP = {
    1: (MOUSEEVENTF_LEFTUP, 0),
    2: (MOUSEEVENTF_RIGHTUP, 0),
    3: (MOUSEEVENTF_MIDDLEUP, 0),
    4: (MOUSEEVENTF_XUP, XBUTTON1),
    5: (MOUSEEVENTF_XUP, XBUTTON2),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # All three members are needed so sizeof(INPUT) matches Windows (40 on x64);
    # SendInput rejects the whole call when cbSize is wrong.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# (HMONITOR, HDC, monitor rectangle, LPARAM) -> keep going
_MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


# Private DLL instances, so prototypes set here never clash with the ones pynput
# and mss set on the shared ctypes.windll objects.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shcore = ctypes.WinDLL("shcore", use_last_error=True)


def _bind(dll, name, restype, *argtypes):
    # Explicit argtypes/restype on everything: ctypes' default int would truncate
    # 64-bit handles.
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_SetProcessDpiAwarenessContext = _bind(_user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, ctypes.c_void_p)
_GetThreadDpiAwarenessContext = _bind(_user32, "GetThreadDpiAwarenessContext", ctypes.c_void_p)
_GetAwarenessFromDpiAwarenessContext = _bind(
    _user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, ctypes.c_void_p
)
_GetSystemMetrics = _bind(_user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_MonitorFromPoint = _bind(_user32, "MonitorFromPoint", ctypes.c_void_p, wintypes.POINT, wintypes.DWORD)
_EnumDisplayMonitors = _bind(
    _user32, "EnumDisplayMonitors", wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM
)
_GetDpiForMonitor = _bind(
    _shcore,
    "GetDpiForMonitor",
    ctypes.c_long,  # HRESULT, checked by hand (ctypes.HRESULT would raise)
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
)
_GetCursorPos = _bind(_user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_GetForegroundWindow = _bind(_user32, "GetForegroundWindow", wintypes.HWND)
_SetForegroundWindow = _bind(_user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
_SetWindowPos = _bind(
    _user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
)
_IsWindow = _bind(_user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_IsIconic = _bind(_user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
_ShowWindowAsync = _bind(_user32, "ShowWindowAsync", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_GetWindowThreadProcessId = _bind(
    _user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
)
_AttachThreadInput = _bind(
    _user32, "AttachThreadInput", wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL
)
_GetCurrentThreadId = _bind(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
_GetWindowTextW = _bind(_user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_WindowFromPoint = _bind(_user32, "WindowFromPoint", wintypes.HWND, wintypes.POINT)
_GetAncestor = _bind(_user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_GetAsyncKeyState = _bind(_user32, "GetAsyncKeyState", wintypes.SHORT, ctypes.c_int)
_SendInput = _bind(_user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)


def setup_dpi():
    # The Set call fails when awareness is already set (manifest or an earlier call),
    # so only the resulting awareness counts. PMv1 and PMv2 both report 2.
    _SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    got = _GetAwarenessFromDpiAwarenessContext(_GetThreadDpiAwarenessContext())
    if got != DPI_AWARENESS_PER_MONITOR_AWARE:
        raise RuntimeError(f"could not enable per-monitor DPI awareness (got {got})")


def screen_size():
    return _GetSystemMetrics(SM_CXSCREEN), _GetSystemMetrics(SM_CYSCREEN)


def screens():
    # Every screen's (left, top, right, bottom) in physical pixels, sorted. The main
    # screen is (0, 0, w, h): Windows puts its top-left corner at (0, 0).
    rects = []

    def add(_hmon, _hdc, rect, _data):
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    proc = _MONITORENUMPROC(add)  # referenced until the call returns
    if not _EnumDisplayMonitors(None, None, proc, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return tuple(sorted(rects))


def primary_dpi():
    # (0, 0) is always the primary monitor's top-left corner.
    mon = _MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
    dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
    hr = _GetDpiForMonitor(mon, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    if hr != 0:
        raise OSError(f"GetDpiForMonitor failed (HRESULT {hr & 0xFFFFFFFF:#010x})")
    return dpi_x.value


def display():
    # The main screen's size and DPI, plus every screen's rectangle: the board's
    # absolute mouse reaches the main screen only, and any change here invalidates
    # a recording or replay in progress.
    w, h = screen_size()
    return w, h, primary_dpi(), screens()


def px_to_hid(p, size):
    # Exact at both edges, at most 1 px off in between.
    return max(0, min(HID_MAX, round(p * HID_MAX / (size - 1))))


def hid_to_px(v, size):
    return round(v * (size - 1) / HID_MAX)


def clamp_point(x, y, w, h):
    return min(max(x, 0), w - 1), min(max(y, 0), h - 1)


def cursor_pos():
    # Fails (returns None) while the secure desktop (UAC, lock screen) is active.
    pt = wintypes.POINT()
    if not _GetCursorPos(ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def foreground_window():
    return _GetForegroundWindow() or 0


def root_window_at(x, y):
    hwnd = _WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return 0
    return _GetAncestor(hwnd, GA_ROOT) or 0


def set_foreground(hwnd):
    # Nothing here waits for the target's thread: the Player calls this holding its
    # lock, and a hung target app must not block Stop, the hotkeys or the board reader.
    if not hwnd or not _IsWindow(hwnd):
        return False
    if _IsIconic(hwnd):
        _ShowWindowAsync(hwnd, SW_RESTORE)
    _SetForegroundWindow(hwnd)
    if foreground_window() != hwnd:
        _force_foreground(hwnd)
    return foreground_window() == hwnd


def _force_foreground(hwnd):
    # The foreground lock refuses SetForegroundWindow from a background process;
    # sharing input state with the current foreground thread lifts it.
    me = _GetCurrentThreadId()
    fg = _GetWindowThreadProcessId(_GetForegroundWindow(), None)
    attached = bool(fg) and fg != me and bool(_AttachThreadInput(me, fg, True))
    try:
        _SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_ASYNCWINDOWPOS)
        _SetForegroundWindow(hwnd)
    finally:
        if attached:
            _AttachThreadInput(me, fg, False)


def window_title(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    if _GetWindowTextW(hwnd, buf, len(buf)) <= 0:
        return ""
    return buf.value


def key_down(vk):
    return bool(_GetAsyncKeyState(vk) & 0x8000)


def buttons_swapped():
    # "Primary mouse button: Right" in Settings.
    return bool(_GetSystemMetrics(SM_SWAPBUTTON))


def button_vk(code):
    # Button codes are physical (hooks.py undoes the swap), and so is GetAsyncKeyState.
    return _BUTTON_VK[code]


def _key_up_input(scan, ext):
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wScan=scan, dwFlags=flags)))


def _button_up_input(code):
    flags, data = _BUTTON_UP[code]
    return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(mouseData=data, dwFlags=flags)))


def sendinput_release(keys, buttons):
    # Last resort when the board vanished while holding something: Windows can keep
    # a key or button down after its device is gone.
    try:
        inputs = [_key_up_input(*k) for k in keys if k is not None]
        inputs += [_button_up_input(code) for code in buttons]
        if not inputs:
            return
        log.info("SendInput release: %d keys/buttons", len(inputs))
        sent = _SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))
        if sent != len(inputs):
            log.warning("SendInput released %d of %d (error %d)", sent, len(inputs), ctypes.get_last_error())
    except Exception:
        log.exception("SendInput release failed")


def main_monitor(monitors):
    # mss lists every screen after its all-screens entry, in Windows' enumeration
    # order, which need not start with the main screen.
    return next((m for m in monitors[1:] if m["left"] == 0 and m["top"] == 0), monitors[1])


def screenshot_png():
    # A new MSS instance per call keeps this safe from any thread. Imported here so
    # nothing loads mss before setup_dpi().
    try:
        import mss
        import mss.tools

        with mss.MSS() as sct:
            img = sct.grab(main_monitor(sct.monitors))
            return mss.tools.to_png(img.rgb, img.size)
    except Exception as e:
        log.warning("screenshot failed: %r", e)
        return None

"""Talks to the PiComp board's data port directly, for firmware bring-up.

Stop the server first: only one program can have the COM port open.

    python tools/fwcheck.py ports                      list the board's COM ports
    python tools/fwcheck.py hello                      HELLO and PING round trips
    python tools/fwcheck.py corners [--dwell 1.0]      cursor to the corners and the centre, checked with GetCursorPos
    python tools/fwcheck.py click X Y [--button 1]     click at pixel X,Y
    python tools/fwcheck.py scroll V H                 wheel notches at the cursor (V: + is up, H: + is right)
    python tools/fwcheck.py keyscan [--gap 0.08]       press every key, compare what the hook sees with keymap.py
    python tools/fwcheck.py hold USAGE [--seconds 10] [--no-ping] [--wait 3]
                                                       hold one key, count the key-downs the hook sees
    python tools/fwcheck.py keys USAGE... [--gap 0.05] [--wait 3]
                                                       tap keys in order: "keys 11 8 15 15 18" types "hello"
    python tools/fwcheck.py send LINE... [--no-hello] [--listen 0.5]
                                                       raw lines: send --no-hello "M 0 0" gives ERR M no-session

Every command except ports takes --port COMx instead of auto-detecting the data port.
USAGE is a HID usage in decimal or hex (11 or 0x0B).

scroll takes V before H, like the wire command "W v h" (PLAN.md M3a writes its
examples H first): "scroll -3 0" is three notches down, "scroll 0 3" three to the right.

keyscan swallows ALL keyboard input, system-wide, while it runs (about 20 s):
nothing you type reaches any app, and Ctrl+C does not work until it ends. Do not
touch the keyboard meanwhile. Run it twice, once with NumLock on and once off,
because Shift+arrow fake shifts depend on NumLock.
"""
import argparse
import collections
import contextlib
import sys
import time
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from picomp import board, keymap, win

PING_INTERVAL = 0.25
KEY_DOWN_MSGS = (0x100, 0x104)  # WM_KEYDOWN, WM_SYSKEYDOWN
LLKHF_EXTENDED = 0x01
LLKHF_ANY_INJECTED = 0x10 | 0x02  # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
SCAN_USAGES = list(range(4, 102)) + list(range(224, 232))
SHIFT_RIGHT = ((True, 225), (True, 79), (False, 79), (False, 225))


def find_data_port():
    ports = board.find_data_ports()
    if len(ports) != 1:
        sys.exit(f"expected exactly one board data port (location ending {board.DATA_ITF_SUFFIX}), "
                 f"found {len(ports)}; run 'ports' or pass --port")
    return ports[0]


def ms_since(t0):
    return (time.perf_counter() - t0) * 1000


class Link:
    # Synchronous line link. Only complete lines are acted on, and PING goes out
    # only from pump(), so each command controls the board's heartbeat exactly.

    def __init__(self, ser, port):
        self.ser = ser
        self.port = port
        self.lines = board.LineBuffer()
        self.pending = collections.deque()
        self.t0 = time.perf_counter()
        self.next_ping = 0.0

    def stamp(self):
        return f"+{time.perf_counter() - self.t0:.3f} s"

    def send(self, line):
        self.ser.write((line + "\n").encode())

    def _take(self, block):
        # block: wait up to the port timeout (0.1 s) for the first byte.
        n = self.ser.in_waiting
        data = self.ser.read(n or 1) if n or block else b""
        self.pending.extend(self.lines.feed(data))

    def read_line(self, deadline):
        while not self.pending:
            if time.perf_counter() >= deadline:
                return None
            self._take(block=True)
        return self.pending.popleft()

    def request(self, cmd, expect, timeout=1.0):
        # Returns the first reply starting with expect, skipping anything else.
        self.send(cmd)
        deadline = time.perf_counter() + timeout
        while True:
            line = self.read_line(deadline)
            if line is None or line.startswith(expect):
                return line
            print(f"  (skipped: {line!r})")

    def pump(self, seconds, ping=True):
        # Polls instead of blocking in read(), so the gaps between commands stay exact.
        # The PONGs that answer our own PINGs are not printed.
        end = time.perf_counter() + seconds
        seen = []
        while True:
            now = time.perf_counter()
            if ping and now >= self.next_ping:
                self.next_ping = now + PING_INTERVAL
                self.send(board.PING)
            self._take(block=False)
            while self.pending:
                line = self.pending.popleft()
                seen.append(line)
                if not (ping and line == "PONG"):
                    print(f"  {self.stamp()}  < {line}")
            if now >= end:
                return seen
            time.sleep(min(end - now, 0.005))


def say_hello(link):
    t0 = time.perf_counter()
    try:
        fw = board.handshake(link.ser, link.lines, on_skip=lambda line: print(f"  (skipped: {line!r})"))
    except board.HandshakeError as e:
        sys.exit(f"{link.port}: {e}")
    print(f"{link.port}: OK PICOMP {board.PROTO_VERSION} {fw}  (handshake {ms_since(t0):.1f} ms)")


def open_port(args):
    port = args.port or find_data_port()
    ser = serial.Serial(port, timeout=0.1, write_timeout=1)
    ser.dtr = True  # the firmware only replies, and only holds a session, while DTR is set
    return Link(ser, port)


@contextlib.contextmanager
def board_session(args):
    link = open_port(args)
    with link.ser:
        say_hello(link)
        link.t0 = time.perf_counter()
        try:
            yield link
        finally:
            try:
                link.send(board.RELEASE)
            except serial.SerialException as e:
                print(f"{link.port}: could not send R: {e}")


def dpi_aware():
    try:
        win.setup_dpi()
    except RuntimeError as e:
        sys.exit(str(e))


def key_lines(usage):
    try:
        return board.fmt_key(True, usage), board.fmt_key(False, usage)
    except ValueError as e:
        sys.exit(str(e))


def tap(link, usage, gap):
    link.send(board.fmt_key(True, usage))
    link.pump(gap)
    link.send(board.fmt_key(False, usage))
    link.pump(gap)


def countdown(seconds, what):
    print(f"{what}: starting in", end="", flush=True)
    for i in range(seconds, 0, -1):
        print(f" {i}", end="", flush=True)
        time.sleep(1)
    print(" go")


def cmd_ports(_args):
    ports = board.board_ports()
    if not ports:
        print(f"no board ports found (VID {board.BOARD_VID:04X}, PID {board.BOARD_PID:04X})")
        return 1
    for p in ports:
        role = "data" if (p.location or "").endswith(board.DATA_ITF_SUFFIX) else "other (REPL?)"
        print(f"{p.device:7} {role:14} location={p.location} serial={p.serial_number} "
              f"description={p.description!r}")
    return 0


def cmd_hello(args):
    with board_session(args) as link:
        t0 = time.perf_counter()
        pong = link.request(board.PING, "PONG")
        print(f"{link.port}: {pong or 'no reply to PING'}  ({ms_since(t0):.1f} ms)")
        return 0 if pong else 1


def cmd_corners(args):
    dpi_aware()
    w, h = win.screen_size()
    points = [(0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1), (w // 2, h // 2)]
    bad = 0
    with board_session(args) as link:
        print(f"screen {w}x{h} physical px")
        for x, y in points:
            hx, hy = win.px_to_hid(x, w), win.px_to_hid(y, h)
            link.send(board.fmt_move(hx, hy))
            link.pump(args.dwell)
            pos = win.cursor_pos()
            off = pos is None or max(abs(pos[0] - x), abs(pos[1] - y)) > 1
            bad += off
            print(f"  target ({x}, {y})  HID ({hx}, {hy})  cursor {pos}  {'OFF' if off else 'ok'}")
    print("all corners ok" if not bad else f"{bad} of {len(points)} points OFF by more than 1 px")
    return 1 if bad else 0


def cmd_click(args):
    dpi_aware()
    w, h = win.screen_size()
    if not (0 <= args.x < w and 0 <= args.y < h):
        sys.exit(f"X,Y must be on the screen: 0..{w - 1}, 0..{h - 1}")
    hx, hy = win.px_to_hid(args.x, w), win.px_to_hid(args.y, h)
    with board_session(args) as link:
        link.send(board.fmt_move(hx, hy))
        link.send(board.fmt_button(True, args.button, hx, hy))
        link.pump(0.05)
        link.send(board.fmt_button(False, args.button, hx, hy))
        link.pump(0.3)
        print(f"clicked button {args.button} at ({args.x}, {args.y}), HID ({hx}, {hy}); "
              f"cursor now {win.cursor_pos()}")
    return 0


def cmd_scroll(args):
    for name, n in (("V", args.v), ("H", args.h)):
        if not -127 <= n <= 127:
            sys.exit(f"{name} must be in -127..127")
    dpi_aware()
    sw, sh = win.screen_size()
    pos = win.cursor_pos()
    if pos is None:
        sys.exit("GetCursorPos failed (secure desktop?)")
    x, y = win.clamp_point(pos[0], pos[1], sw, sh)
    hx, hy = win.px_to_hid(x, sw), win.px_to_hid(y, sh)
    with board_session(args) as link:
        link.send(board.fmt_wheel(args.v, args.h, hx, hy))
        link.pump(0.3)
        print(f"sent {args.v} vertical notch(es) (+ is up) and {args.h} horizontal (+ is right) at ({x}, {y})")
    return 0


def ev_key(ev):
    return ev[3], bool(ev[4] & LLKHF_EXTENDED)


def ev_injected(ev):
    return bool(ev[4] & LLKHF_ANY_INJECTED)


def ev_fake(ev):
    return keymap.is_fake_shift(*ev_key(ev), ev[2])


def is_down(ev):
    return ev[1] in KEY_DOWN_MSGS


def key_text(scan, ext):
    return f"{'E0 ' if ext else ''}{scan:02X}"


def ev_text(ev):
    if ev is None:
        return "-"
    return f"{key_text(*ev_key(ev))} vk {ev[2]:02X}{' inj' if ev_injected(ev) else ''}"


def report_keyscan(events, starts):
    # Each usage owns the events between its KD and the next usage's KD.
    print()
    print(f"{'usage':6} {'keymap':14} {'down':18} {'up':18} {'records as':11} result")
    mismatches = unseen = injected = 0
    by_key = {}
    for i, u in enumerate(SCAN_USAGES):
        window = [e for e in events if starts[i] <= e[0] < starts[i + 1]]
        real = [e for e in window if not ev_fake(e)]
        down = next((e for e in real if is_down(e)), None)
        up = next((e for e in real if not is_down(e)), None)
        want = keymap.scan_for(u)
        if down is None and up is None:
            result = "none seen"
            unseen += want is not None
        elif want is None:
            result = "-"
        elif down is not None and up is not None and ev_key(down) == want == ev_key(up):
            result = "ok"
        else:
            result = "MISMATCH"
            mismatches += 1
        if any(ev_injected(e) for e in window):
            result += " INJECTED"
            injected += 1
        records = "-"
        if down is not None:
            by_key.setdefault(ev_key(down), []).append(u)
            usage = keymap.usage_for(*ev_key(down))
            records = "unmapped" if usage is None else f"0x{usage:02X}"
        want_text = key_text(*want) if want else "not in keymap"
        print(f"0x{u:02X}   {want_text:14} {ev_text(down):18} {ev_text(up):18} {records:11} {result}")
        for e in window:
            if e is not down and e is not up:
                fake = "  (fake shift)" if ev_fake(e) else ""
                print(f"         also {'down' if is_down(e) else 'up'}: {ev_text(e)}{fake}")

    print()
    shared = {k: us for k, us in by_key.items() if len(us) > 1}
    if shared:
        print("collisions (one scan code from several usages):")
        for k, us in sorted(shared.items()):
            print(f"  {key_text(*k)}: {', '.join(f'0x{u:02X}' for u in us)}")
    else:
        print("no collisions")
    print(f"{mismatches} mismatches, {unseen} keymap usages not seen, {injected} usages flagged injected")
    return mismatches + unseen + injected


def report_shift(events, lo, hi):
    print()
    print("Shift+Right from the board (KD 225, KD 79, KU 79, KU 225):")
    shown = [e for e in events if lo <= e[0] < hi]
    for e in shown:
        fake = "  FAKE SHIFT" if ev_fake(e) else ""
        print(f"  +{(e[0] - lo) * 1000:6.1f} ms  {'down' if is_down(e) else 'up  '}  {ev_text(e)}{fake}")
    if not shown:
        print("  no events seen")


def cmd_keyscan(args):
    from pynput import keyboard

    events = []
    listener = None

    def record(msg, data):
        events.append((time.perf_counter(), msg, data.vkCode, data.scanCode, data.flags))
        listener.suppress_event()  # raises; nothing reaches any app

    with board_session(args) as link:
        print("keyboard input is now swallowed until the scan ends (about 20 s): do not type")
        listener = keyboard.Listener(win32_event_filter=record)
        listener.start()
        listener.wait()
        starts = []
        try:
            for u in SCAN_USAGES:
                print(f"{u:02X}", end=" ", flush=True)
                starts.append(time.perf_counter())
                tap(link, u, args.gap)
            print()
            starts.append(time.perf_counter())
            for down, u in SHIFT_RIGHT:
                link.send(board.fmt_key(down, u))
                link.pump(args.gap)
            link.pump(0.2)
            shift_end = time.perf_counter()
        finally:
            try:
                link.send(board.RELEASE)
                time.sleep(0.1)  # let the hook swallow anything R releases
            finally:
                listener.stop()
    print("keyboard released")
    problems = report_keyscan(events, starts)
    report_shift(events, starts[-1], shift_end)
    return 1 if problems else 0


def cmd_hold(args):
    from pynput import keyboard

    kd, ku = key_lines(args.usage)
    target = keymap.scan_for(args.usage)
    if target is None:
        print(f"usage 0x{args.usage:02X} is not in keymap.py, so the hook count will stay 0")
    seen = {"downs": 0, "injected": 0, "last": None}

    def count(msg, data):
        if msg in KEY_DOWN_MSGS and (data.scanCode, bool(data.flags & LLKHF_EXTENDED)) == target:
            seen["downs"] += 1
            seen["last"] = time.perf_counter()
            if data.flags & LLKHF_ANY_INJECTED:
                seen["injected"] += 1
        return False  # not suppressed; False only skips pynput's own callbacks

    with board_session(args) as link:
        countdown(args.wait, f"focus the window that should get usage 0x{args.usage:02X}")
        listener = keyboard.Listener(win32_event_filter=count)
        listener.start()
        listener.wait()
        t_kd = time.perf_counter()
        try:
            link.send(kd)
            print(f"  {link.stamp()}  > {kd}  (holding {args.seconds:g} s{', no PING' if args.no_ping else ''})")
            end = t_kd + args.seconds
            while True:
                left = end - time.perf_counter()
                if left <= 0:
                    break
                link.pump(min(1.0, left), ping=not args.no_ping)
                print(f"  {link.stamp()}  hook: {seen['downs']} key-downs ({seen['injected']} injected)")
        finally:
            try:
                link.send(ku)
                link.send(board.RELEASE)
            finally:
                listener.stop()
    last = "none" if seen["last"] is None else f"+{seen['last'] - t_kd:.3f} s after KD"
    print(f"total: {seen['downs']} key-downs seen by the hook ({seen['injected']} injected), last one {last}")
    return 0


def cmd_keys(args):
    for u in args.usages:
        key_lines(u)
    with board_session(args) as link:
        countdown(args.wait, "focus the window that should get the keys")
        for u in args.usages:
            tap(link, u, args.gap)
    return 0


def cmd_send(args):
    link = open_port(args)
    with link.ser:
        link.ser.reset_input_buffer()
        if not args.no_hello:
            say_hello(link)
        link.t0 = time.perf_counter()
        for line in args.lines:
            print(f"  {link.stamp()}  > {line}")
            link.send(line)
        link.pump(args.listen, ping=False)
    return 0


def hid_usage(text):
    try:
        return int(text)
    except ValueError:
        return int(text, 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    port = argparse.ArgumentParser(add_help=False)
    port.add_argument("--port", help="COM port to use instead of auto-detecting the data port")

    sub.add_parser("ports").set_defaults(fn=cmd_ports)

    sub.add_parser("hello", parents=[port]).set_defaults(fn=cmd_hello)

    p = sub.add_parser("corners", parents=[port])
    p.add_argument("--dwell", type=float, default=1.0, help="seconds at each point (default 1.0)")
    p.set_defaults(fn=cmd_corners)

    p = sub.add_parser("click", parents=[port])
    p.add_argument("x", type=int, metavar="X")
    p.add_argument("y", type=int, metavar="Y")
    p.add_argument("--button", type=int, choices=range(1, 6), default=1,
                   help="1 left, 2 right, 3 middle, 4 back, 5 forward (default 1)")
    p.set_defaults(fn=cmd_click)

    p = sub.add_parser("scroll", parents=[port])
    p.add_argument("v", type=int, metavar="V", help="vertical notches, + is up")
    p.add_argument("h", type=int, metavar="H", help="horizontal notches, + is right")
    p.set_defaults(fn=cmd_scroll)

    p = sub.add_parser("keyscan", parents=[port])
    p.add_argument("--gap", type=float, default=0.08, help="seconds after each KD and KU (default 0.08)")
    p.set_defaults(fn=cmd_keyscan)

    p = sub.add_parser("hold", parents=[port])
    p.add_argument("usage", type=hid_usage, metavar="USAGE")
    p.add_argument("--seconds", type=float, default=10.0, help="how long to hold (default 10)")
    p.add_argument("--no-ping", action="store_true", help="send no PING, so the board's 1 s heartbeat fires")
    p.add_argument("--wait", type=int, default=3, help="countdown before pressing (default 3)")
    p.set_defaults(fn=cmd_hold)

    p = sub.add_parser("keys", parents=[port])
    p.add_argument("usages", type=hid_usage, nargs="+", metavar="USAGE")
    p.add_argument("--gap", type=float, default=0.05, help="seconds after each KD and KU (default 0.05)")
    p.add_argument("--wait", type=int, default=3, help="countdown before typing (default 3)")
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser("send", parents=[port])
    p.add_argument("lines", nargs="+", metavar="LINE", help="raw protocol line, quoted, e.g. \"M 0 0\"")
    p.add_argument("--no-hello", action="store_true", help="skip the handshake (no session)")
    p.add_argument("--listen", type=float, default=0.5, help="seconds to print replies (default 0.5)")
    p.set_defaults(fn=cmd_send)

    args = ap.parse_args()
    try:
        code = args.fn(args)
    except serial.SerialException as e:
        code = f"serial error: {e}\n(if the port is busy, stop the server first: the COM port is exclusive)"
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()

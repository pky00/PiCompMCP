"""Serial link to the PiComp board's data port (protocol version 1).

The pure helpers (fmt_*, parse_reply, LineBuffer, handshake) are shared with
tools/fwcheck.py. BoardLink is the server's connection and its reader thread.
"""
import logging
import threading
import time

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

BOARD_VID = 0x2E8A
BOARD_PID = 0x0005
DATA_ITF_SUFFIX = ":x.2"  # data CDC is USB interface 2; the REPL CDC is interface 0
PROTO_VERSION = 1
HID_MAX = 32767

HELLO = "HELLO"
PING = "PING"
RELEASE = "R"

PING_INTERVAL_S = 0.25
PONG_TIMEOUT_S = 1.5
RETRY_S = 1.0
NO_BOARD = "no board found"
CLOSED = "closed: the server is stopping"

_SIMPLE_REPLIES = {"PONG": "pong", "!HALT": "halt", "!TIMEOUT": "timeout"}


def board_ports():
    return [p for p in list_ports.comports() if p.vid == BOARD_VID and p.pid == BOARD_PID]


def find_data_ports():
    # Never trust a COM number, and never pick the REPL port (":x.0").
    return [p.device for p in board_ports() if (p.location or "").endswith(DATA_ITF_SUFFIX)]


def _is_int(value):
    # bool is an int subclass, but would format as "True".
    return isinstance(value, int) and not isinstance(value, bool)


def _check(value, lo, hi, what):
    if not _is_int(value) or not lo <= value <= hi:
        raise ValueError(f"{what} must be an integer in {lo}..{hi}, got {value!r}")


def _check_xy(hx, hy):
    _check(hx, 0, HID_MAX, "x")
    _check(hy, 0, HID_MAX, "y")


def fmt_move(hx, hy):
    _check_xy(hx, hy)
    return f"M {hx} {hy}"


def fmt_button(down, code, hx, hy):
    _check(code, 1, 5, "button")
    _check_xy(hx, hy)
    return f"{'BD' if down else 'BU'} {code} {hx} {hy}"


def fmt_wheel(v, h, hx, hy):
    _check(v, -127, 127, "vertical wheel")
    _check(h, -127, 127, "horizontal wheel")
    _check_xy(hx, hy)
    return f"W {v} {h} {hx} {hy}"


def fmt_key(down, usage):
    if not _is_int(usage) or not (4 <= usage <= 101 or 224 <= usage <= 231):
        raise ValueError(f"key usage must be 4..101 or 224..231, got {usage!r}")
    return f"{'KD' if down else 'KU'} {usage}"


class LineBuffer:
    # Only complete "\n"-terminated lines are returned: pyserial's readline()
    # hands back half a line when it times out.

    def __init__(self):
        self._buf = b""
        self._back = []

    def feed(self, data):
        lines, self._back = self._back, []
        if data:
            *done, self._buf = (self._buf + data).split(b"\n")
            for raw in done:
                line = raw.decode("ascii", errors="replace").replace("\r", "")
                if line:
                    lines.append(line)
        return lines

    def unread(self, lines):
        # The next feed() returns these first.
        self._back = list(lines) + self._back


def parse_reply(line):
    parts = line.split(" ")
    if parts[0] == "OK":
        if len(parts) == 4 and parts[1] == "PICOMP" and parts[2].isascii() and parts[2].isdigit():
            return "ok", (int(parts[2]), parts[3])
        return "other", line
    if line in _SIMPLE_REPLIES:
        return _SIMPLE_REPLIES[line], None
    if parts[0] == "ERR" and len(parts) == 3:
        return "err", (parts[1], parts[2])
    return "other", line


class HandshakeError(Exception):
    pass


def handshake(ser, lines, timeout=1.0, clock=time.perf_counter, on_skip=None):
    # Lines left over from an earlier session (PONG, !HALT, ERR ...) are skipped
    # until the OK. Anything after the OK stays in `lines` for the caller.
    ser.reset_input_buffer()
    ser.write((HELLO + "\n").encode())
    deadline = clock() + timeout
    while clock() < deadline:
        got = lines.feed(ser.read(ser.in_waiting or 1))
        for i, line in enumerate(got):
            kind, detail = parse_reply(line)
            if kind != "ok":
                if on_skip is not None:
                    on_skip(line)
                continue
            lines.unread(got[i + 1:])
            version, fw = detail
            if version != PROTO_VERSION:
                raise HandshakeError(f"board speaks protocol {version}, expected {PROTO_VERSION}")
            return fw
    raise HandshakeError(f"no reply to HELLO within {timeout:g} s")


def _close_quietly(ser):
    if ser is None:
        return
    try:
        ser.close()
    except Exception:
        log.debug("closing the port failed", exc_info=True)


class BoardLink:
    # Only the reader thread opens and closes the port; send() works from any
    # thread. on_event is always called without a BoardLink lock held.

    def __init__(self, on_event=None, ping_ok=None):
        self._on_event = on_event
        self._ping_ok = ping_ok
        self._wlock = threading.Lock()
        self._hello = threading.Event()
        self._thread = None
        self._closing = False
        self._ser = None
        self._lines = None
        self._broken = False
        self._halted = False
        self._ping_sent_at = None
        self._state = ("offline", NO_BOARD)  # replaced whole, so readers see a consistent pair
        self.port = None
        self.fw_version = None

    @property
    def online(self):
        return self._ser is not None

    @property
    def status(self):
        return self._state[0]

    @property
    def detail(self):
        return self._state[1]

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="board-reader", daemon=True)
            self._thread.start()

    def close(self):
        self._closing = True
        self.send(RELEASE)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
            if t.is_alive():
                log.warning("board reader did not stop within 2 s")
        self._drop_port()  # no-op once the reader has closed it

    def send(self, line):
        data = (line + "\n").encode()
        with self._wlock:
            ser = self._ser
            if ser is None or self._broken:
                return False
            try:
                ser.write(data)
            except (serial.SerialException, OSError) as e:
                self._broken = True
                log.warning("write to %s failed: %s", self.port, e)
                return False
        return True

    def hello(self, timeout=1.0):
        # Never from the reader thread: the reader is what sets the event.
        self._hello.clear()
        if not self.send(HELLO):
            return False
        return self._hello.wait(timeout)

    def _set_state(self, status, detail):
        if (status, detail) != self._state:
            log.info("board %s: %s", status, detail)
        self._state = (status, detail)

    def _publish_online(self):
        if self._halted:
            self._set_state("halted", f"{self.port}: halted by BOOT, next Replay re-arms")
        else:
            self._set_state("connected", f"{self.port}, firmware {self.fw_version}")

    def _emit(self, kind, line):
        if self._on_event is None:
            return
        try:
            self._on_event(kind, line)
        except Exception:
            log.exception("board %s handler failed", kind)

    def _should_ping(self):
        if self._ping_ok is None:
            return True
        try:
            return bool(self._ping_ok())
        except Exception:
            log.exception("ping_ok failed")
            return False

    def _nap(self, seconds):
        end = time.perf_counter() + seconds
        while not self._closing:
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep(min(left, 0.1))

    def _run(self):
        try:
            while not self._closing:
                try:
                    if self._ser is not None:
                        self._serve()
                    elif not self._connect():
                        self._nap(RETRY_S)
                except Exception:
                    log.exception("board reader error")
                    if self._ser is not None:
                        self._lose("reader error")
                    self._nap(RETRY_S)
        finally:
            self._drop_port()
            self._set_state("offline", CLOSED if self._closing else NO_BOARD)

    def _connect(self):
        ports = find_data_ports()
        if not ports:
            self._set_state("offline", NO_BOARD)
            return False
        error = None
        for dev in ports:
            if self._closing:
                return False
            ser = None
            lines = LineBuffer()
            try:
                ser = serial.Serial(port=dev, timeout=0.1, write_timeout=1)
                ser.dtr = True
                fw = handshake(ser, lines, on_skip=lambda line: log.debug("skipped stale line %r", line))
            except (serial.SerialException, OSError, HandshakeError) as e:
                error = f"{dev}: {e}"
                _close_quietly(ser)
                continue
            if self._closing:
                _close_quietly(ser)
                return False
            self.port = dev
            self.fw_version = fw
            self._lines = lines
            self._halted = False
            self._broken = False
            self._ping_sent_at = None
            self._ser = ser
            self._publish_online()
            self._emit("connected", "")
            return True
        self._set_state("error", error)
        return False

    def _serve(self):
        ser = self._ser
        next_ping = 0.0
        while not self._closing:
            try:
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                self._lose(f"read failed: {e}")
                return
            for line in self._lines.feed(data):
                self._handle(line)
            if self._broken:
                self._lose("write failed")
                return
            now = time.perf_counter()
            if now >= next_ping:
                next_ping = now + PING_INTERVAL_S
                # A hung server stops pinging, so the board releases everything after 1 s.
                if self._should_ping() and self.send(PING) and self._ping_sent_at is None:
                    self._ping_sent_at = now
            # Only PINGs actually sent count, so suppressed pings never look like a loss.
            sent = self._ping_sent_at
            if sent is not None and now - sent > PONG_TIMEOUT_S:
                self._lose(f"no PONG for {PONG_TIMEOUT_S:g} s")
                return

    def _handle(self, line):
        kind, _ = parse_reply(line)
        if kind == "pong":
            self._ping_sent_at = None
        elif kind == "ok":
            self._halted = False
            self._publish_online()
            self._hello.set()
        elif kind == "halt":
            log.warning("board halted (BOOT pressed)")
            self._halted = True
            self._publish_online()
            self._emit("halt", line)
        elif kind == "timeout":
            log.warning("board heartbeat timeout: it released everything")
            self._emit("timeout", line)
        elif kind == "err":
            log.warning("board replied %s", line)
            self._emit("err", line)
        else:
            log.warning("unexpected line from the board: %r", line)

    def _drop_port(self):
        with self._wlock:
            ser, self._ser = self._ser, None
            _close_quietly(ser)

    def _lose(self, why):
        port = self.port
        self._drop_port()
        self.port = None
        self.fw_version = None
        self._halted = False
        self._ping_sent_at = None
        self._set_state("offline", CLOSED if self._closing else NO_BOARD)
        if self._closing:
            return
        log.warning("board on %s lost: %s", port, why)
        self._emit("lost", "")

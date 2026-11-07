# picomp/board.py: wire formatting, reply parsing, line framing, port discovery,
# the HELLO handshake against a scripted port, and BoardLink's handling of board
# lines. Real connect, reconnect and halt handling are checked on the hardware
# (PLAN.md M6, M8a).
from types import SimpleNamespace

import pytest
import serial

import proto
from picomp import board, keymap


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptedPort:
    # Each read returns (at most n bytes of) the next scripted chunk. Once the
    # script is used up, a read returns nothing and the clock moves on by one
    # port timeout, like a real read that waited and got no data.

    def __init__(self, chunks, clock):
        self.chunks = list(chunks)
        self.clock = clock
        self.log = []
        self.read_times = []

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def reset_input_buffer(self):
        self.log.append("reset")

    def write(self, data):
        self.log.append(data)
        return len(data)

    def read(self, n):
        self.read_times.append(self.clock.now)
        if not self.chunks:
            self.clock.now += 0.125
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > n:
            self.chunks.insert(0, chunk[n:])
        return chunk[:n]


def scripted(*chunks):
    clock = FakeClock()
    return ScriptedPort(chunks, clock), board.LineBuffer(), clock


class FakeWriter:
    def __init__(self, fail=False, on_write=None):
        self.fail = fail
        self.on_write = on_write
        self.attempts = 0
        self.written = []

    def write(self, data):
        self.attempts += 1
        if self.fail:
            raise serial.SerialException("device disconnected")
        self.written.append(data)
        if self.on_write is not None:
            self.on_write(data)
        return len(data)


def online_link(port, on_event=None):
    # The state the reader thread leaves after a successful connect.
    link = board.BoardLink(on_event=on_event)
    link._ser = port
    link.port = "COM7"
    link.fw_version = "0.1.0"
    link._handle("OK PICOMP 1 0.1.0")
    return link


# --- formatting ---

def test_fmt_lines():
    assert board.fmt_move(0, 0) == "M 0 0"
    assert board.fmt_move(32767, 16384) == "M 32767 16384"
    assert board.fmt_button(True, 1, 10, 20) == "BD 1 10 20"
    assert board.fmt_button(False, 5, 0, 32767) == "BU 5 0 32767"
    assert board.fmt_wheel(-2, 0, 16384, 16384) == "W -2 0 16384 16384"
    assert board.fmt_wheel(127, -127, 1, 2) == "W 127 -127 1 2"
    assert board.fmt_key(True, 4) == "KD 4"
    assert board.fmt_key(False, 231) == "KU 231"


def test_constants():
    assert (board.HELLO, board.PING, board.RELEASE) == ("HELLO", "PING", "R")
    assert board.PROTO_VERSION == proto.PROTO_VERSION == 1
    assert (board.BOARD_VID, board.BOARD_PID, board.DATA_ITF_SUFFIX) == (0x2E8A, 0x0005, ":x.2")


@pytest.mark.parametrize("fn, args", [
    (board.fmt_move, (-1, 0)),
    (board.fmt_move, (0, 32768)),
    (board.fmt_move, (1.0, 0)),
    (board.fmt_move, (True, 0)),
    (board.fmt_button, (True, 0, 0, 0)),
    (board.fmt_button, (True, 6, 0, 0)),
    (board.fmt_button, (False, 1, 32768, 0)),
    (board.fmt_button, (False, 1, 0, -1)),
    (board.fmt_wheel, (128, 0, 0, 0)),
    (board.fmt_wheel, (0, -128, 0, 0)),
    (board.fmt_wheel, (0, 0, 0, 32768)),
    (board.fmt_wheel, (0.5, 0, 0, 0)),
    (board.fmt_key, (True, 3)),
    (board.fmt_key, (True, 102)),
    (board.fmt_key, (True, 223)),
    (board.fmt_key, (False, 232)),
    (board.fmt_key, (True, True)),
    (board.fmt_key, (True, "4")),
])
def test_fmt_rejects_bad_values(fn, args):
    with pytest.raises(ValueError):
        fn(*args)


def test_formatted_lines_parse_on_the_firmware():
    cases = [
        (board.fmt_move(0, 32767), ("M", (0, 32767), None)),
        (board.fmt_button(True, 5, 1, 2), ("BD", (5, 1, 2), None)),
        (board.fmt_button(False, 1, 32767, 0), ("BU", (1, 32767, 0), None)),
        (board.fmt_wheel(-127, 127, 3, 4), ("W", (-127, 127, 3, 4), None)),
        (board.fmt_key(True, 4), ("KD", (4,), None)),
        (board.fmt_key(False, 231), ("KU", (231,), None)),
        (board.HELLO, ("HELLO", (), None)),
        (board.PING, ("PING", (), None)),
        (board.RELEASE, ("R", (), None)),
    ]
    for line, parsed in cases:
        assert proto.parse(line.encode()) == parsed


def test_longest_lines_fit_the_firmware_limit():
    for line in (board.fmt_wheel(-127, -127, 32767, 32767), board.fmt_button(False, 5, 32767, 32767)):
        assert len(line.encode()) <= proto.MAX_LINE


@pytest.mark.parametrize("down", [True, False])
def test_fmt_key_accepts_exactly_what_the_firmware_accepts(down):
    for usage in range(-5, 300):
        try:
            board.fmt_key(down, usage)
            sendable = True
        except ValueError:
            sendable = False
        assert sendable == (proto.parse(b"KD %d" % usage)[2] is None), usage


def test_every_keymap_usage_can_be_sent():
    for usage in keymap.USAGE_TO_SCAN:
        assert board.fmt_key(True, usage) == "KD %d" % usage


# --- replies ---

@pytest.mark.parametrize("line, expected", [
    ("OK PICOMP 1 0.1.0", ("ok", (1, "0.1.0"))),
    ("OK PICOMP 2 9.9.9", ("ok", (2, "9.9.9"))),
    ("PONG", ("pong", None)),
    ("!HALT", ("halt", None)),
    ("!TIMEOUT", ("timeout", None)),
    ("ERR KD bad-code", ("err", ("KD", "bad-code"))),
    ("ERR - too-long", ("err", ("-", "too-long"))),
    ("OK PICOMP", ("other", "OK PICOMP")),
    ("OK PICOMP x 0.1.0", ("other", "OK PICOMP x 0.1.0")),
    ("OK PICOMP 1 0.1.0 extra", ("other", "OK PICOMP 1 0.1.0 extra")),
    ("OK OTHER 1 0.1.0", ("other", "OK OTHER 1 0.1.0")),
    ("ERR KD", ("other", "ERR KD")),
    ("pong", ("other", "pong")),
    ("PONG PONG", ("other", "PONG PONG")),
    ("", ("other", "")),
])
def test_parse_reply(line, expected):
    assert board.parse_reply(line) == expected


@pytest.mark.parametrize("cmd, code, echoed", [
    ("KD", "bad-code", "KD"),
    ("M", "halted", "M"),
    ("BD", "no-session", "BD"),
    ("W", "usb-busy", "W"),
    ("-", "too-long", "-"),
    ("-", "internal", "-"),
    ("", "unknown", "-"),
    ("\x01Q", "unknown", "-"),
])
def test_firmware_error_lines_parse_as_err(cmd, code, echoed):
    lines = board.LineBuffer().feed(proto.err_line(cmd, code))
    assert lines == ["ERR %s %s" % (echoed, code)]
    assert board.parse_reply(lines[0]) == ("err", (echoed, code))


# --- LineBuffer ---

def test_line_buffer_returns_only_complete_lines():
    lb = board.LineBuffer()
    assert lb.feed(b"PO") == []
    assert lb.feed(b"NG\r\n!HA") == ["PONG"]
    assert lb.feed(b"LT\nOK PICOMP 1 0.1.0\nERR") == ["!HALT", "OK PICOMP 1 0.1.0"]
    assert lb.feed(b"") == []
    assert lb.feed(b" M halted\n") == ["ERR M halted"]


def test_line_buffer_drops_empty_lines_and_carriage_returns():
    assert board.LineBuffer().feed(b"\n\r\n\nPO\rNG\r\n") == ["PONG"]


def test_line_buffer_replaces_non_ascii():
    assert board.LineBuffer().feed(b"\xffOK\n") == ["�OK"]


def test_unread_lines_come_back_first():
    lb = board.LineBuffer()
    assert lb.feed(b"PAR") == []
    lb.unread(["PONG", "!HALT"])
    assert lb.feed(b"TIAL\n") == ["PONG", "!HALT", "PARTIAL"]
    assert lb.feed(b"") == []


# --- port discovery ---

def test_find_data_ports_picks_interface_2_of_the_board(monkeypatch):
    ports = [
        SimpleNamespace(device="COM6", vid=0x2E8A, pid=0x0005, location="1-1:x.0"),
        SimpleNamespace(device="COM7", vid=0x2E8A, pid=0x0005, location="1-1:x.2"),
        SimpleNamespace(device="COM9", vid=0x2E8A, pid=0x0005, location="1-1:x.12"),
        SimpleNamespace(device="COM8", vid=0x2E8A, pid=0x0005, location=None),
        SimpleNamespace(device="COM3", vid=0x1234, pid=0x0005, location="1-2:x.2"),
        SimpleNamespace(device="COM4", vid=0x2E8A, pid=0x000A, location="1-3:x.2"),
        SimpleNamespace(device="COM1", vid=None, pid=None, location=None),
    ]
    monkeypatch.setattr(board.list_ports, "comports", lambda: ports)
    assert [p.device for p in board.board_ports()] == ["COM6", "COM7", "COM9", "COM8"]
    assert board.find_data_ports() == ["COM7"]


def test_no_board_no_ports(monkeypatch):
    monkeypatch.setattr(board.list_ports, "comports", lambda: [])
    assert board.board_ports() == []
    assert board.find_data_ports() == []


# --- handshake ---

def test_handshake_flushes_then_sends_hello():
    port, lines, clock = scripted(b"OK PICOMP 1 0.1.0\n")
    assert board.handshake(port, lines, clock=clock) == "0.1.0"
    assert port.log == ["reset", b"HELLO\n"]


def test_handshake_skips_stale_lines():
    port, lines, clock = scripted(b"PONG\n!HALT\nERR M halted\n", b"!TIMEOUT\nnoise\nOK PICOMP 1 0.2.0\n")
    skipped = []
    assert board.handshake(port, lines, clock=clock, on_skip=skipped.append) == "0.2.0"
    assert skipped == ["PONG", "!HALT", "ERR M halted", "!TIMEOUT", "noise"]


def test_handshake_joins_a_reply_split_across_reads():
    data = b"OK PICOMP 1 0.1.0\r\n"
    port, lines, clock = scripted(*[data[i:i + 1] for i in range(len(data))])
    assert board.handshake(port, lines, clock=clock) == "0.1.0"


def test_handshake_leaves_later_lines_for_the_caller():
    port, lines, clock = scripted(b"PONG\nOK PICOMP 1 0.1.0\n!HALT\nERR M halted\nPAR")
    assert board.handshake(port, lines, clock=clock) == "0.1.0"
    assert lines.feed(b"TIAL\n") == ["!HALT", "ERR M halted", "PARTIAL"]


def test_handshake_rejects_another_protocol_version():
    port, lines, clock = scripted(b"PONG\nOK PICOMP 2 0.9.0\n")
    with pytest.raises(board.HandshakeError, match="board speaks protocol 2, expected 1"):
        board.handshake(port, lines, clock=clock)


def test_handshake_times_out_without_a_reply():
    port, lines, clock = scripted()
    with pytest.raises(board.HandshakeError) as e:
        board.handshake(port, lines, clock=clock)
    assert str(e.value) == "no reply to HELLO within 1 s"
    assert clock.now >= 1.0
    assert port.read_times and max(port.read_times) < 1.0


def test_handshake_times_out_when_only_stale_lines_arrive():
    port, lines, clock = scripted(b"PONG\n!HALT\nOK PICOMP\n")
    skipped = []
    with pytest.raises(board.HandshakeError):
        board.handshake(port, lines, clock=clock, on_skip=skipped.append)
    assert skipped == ["PONG", "!HALT", "OK PICOMP"]


def test_handshake_timeout_is_configurable():
    port, lines, clock = scripted()
    with pytest.raises(board.HandshakeError, match=r"within 0\.5 s"):
        board.handshake(port, lines, timeout=0.5, clock=clock)
    assert max(port.read_times) < 0.5


# --- BoardLink line handling (no port, no thread) ---

def test_new_link_is_offline():
    link = board.BoardLink()
    assert (link.status, link.detail, link.online) == ("offline", "no board found", False)
    assert link.port is None and link.fw_version is None
    assert link.send(board.PING) is False
    assert link.hello(timeout=0) is False


def test_online_link_reports_port_and_firmware():
    link = online_link(FakeWriter())
    assert link.online
    assert (link.status, link.detail) == ("connected", "COM7, firmware 0.1.0")


def test_send_appends_the_newline():
    port = FakeWriter()
    link = online_link(port)
    assert link.send(board.fmt_move(1, 2)) is True
    assert port.written == [b"M 1 2\n"]


def test_failed_write_marks_the_link_broken():
    port = FakeWriter(fail=True)
    link = online_link(port)
    assert link.send(board.PING) is False
    assert link.send(board.PING) is False
    assert port.attempts == 1


def test_hello_returns_once_the_reader_sees_ok():
    def answer(data):
        if data == b"HELLO\n":
            link._handle("OK PICOMP 1 0.1.0")

    link = online_link(FakeWriter(on_write=answer))
    assert link.hello(timeout=1.0) is True


def test_hello_without_ok_times_out():
    link = online_link(FakeWriter())
    assert link.hello(timeout=0.01) is False


def test_board_lines_become_events_and_status():
    events = []
    link = online_link(FakeWriter(), lambda kind, line: events.append((kind, line)))
    link._handle("!HALT")
    assert link.online
    assert (link.status, link.detail) == ("halted", "COM7: halted by BOOT, next Replay re-arms")
    link._handle("!TIMEOUT")
    link._handle("ERR KD bad-code")
    link._handle("something odd")
    assert events == [("halt", "!HALT"), ("timeout", "!TIMEOUT"), ("err", "ERR KD bad-code")]
    link._handle("OK PICOMP 1 0.1.0")
    assert (link.status, link.detail) == ("connected", "COM7, firmware 0.1.0")
    assert len(events) == 3


def test_pong_clears_the_outstanding_ping():
    link = online_link(FakeWriter())
    link._ping_sent_at = 12.5
    link._handle("PONG")
    assert link._ping_sent_at is None


def test_a_failing_event_handler_is_contained():
    def boom(kind, line):
        raise RuntimeError("handler bug")

    link = online_link(FakeWriter(), boom)
    link._handle("!HALT")
    assert link.status == "halted"

# firmware/proto.py under CPython: line framing, command parsing, the session and
# HALTED gate, modifier codes and ERR lines (PLAN.md section 4).
import pytest

import proto

INPUT_CMDS = ("M", "BD", "BU", "W", "KD", "KU")


def feed_all(*chunks):
    reader = proto.LineReader()
    lines = []
    for chunk in chunks:
        lines += reader.feed(chunk)
    return lines


# --- LineReader ---

def test_complete_lines():
    assert feed_all(b"HELLO\nPING\nM 1 2\n") == [b"HELLO", b"PING", b"M 1 2"]


def test_crlf_is_accepted():
    assert feed_all(b"HELLO\r\nPING\r\n") == [b"HELLO", b"PING"]


def test_empty_lines_are_dropped():
    assert feed_all(b"\n\r\n\nPING\n\n") == [b"PING"]


def test_partial_chunks_are_joined():
    reader = proto.LineReader()
    assert reader.feed(b"M 1") == []
    assert reader.feed(b"6384 2") == []
    assert reader.feed(b"0\nPI") == [b"M 16384 20"]
    assert reader.feed(b"NG\n") == [b"PING"]


def test_byte_at_a_time():
    data = b"BD 1 100 200\nKD 4\n"
    assert feed_all(*[data[i:i + 1] for i in range(len(data))]) == [b"BD 1 100 200", b"KD 4"]


def test_memoryview_chunks_like_the_firmware_feeds():
    rx = bytearray(64)
    data = b"PING\nM 5 6\nKD"
    rx[:len(data)] = data
    reader = proto.LineReader()
    assert reader.feed(memoryview(rx)[:len(data)]) == [b"PING", b"M 5 6"]
    assert reader.feed(memoryview(b" 4\n")) == [b"KD 4"]


def test_line_of_max_length_is_accepted():
    line = b"X" * proto.MAX_LINE
    assert feed_all(line + b"\n") == [line]


def test_carriage_returns_do_not_count_toward_the_limit():
    line = b"X" * proto.MAX_LINE
    assert feed_all(line + b"\r\n") == [line]
    assert feed_all(b"X" * 24 + b"\r" * 30 + b"X" * 24 + b"\n") == [line]


def test_overlong_line_gives_one_too_long_then_recovers():
    assert feed_all(b"X" * (proto.MAX_LINE + 1) + b"\nPING\n") == [proto.TOO_LONG, b"PING"]
    assert feed_all(b"X" * 500 + b"\nPING\n") == [proto.TOO_LONG, b"PING"]


def test_each_overlong_line_is_reported_once():
    data = b"X" * 100 + b"\n" + b"Y" * 60 + b"\r\nPING\n"
    assert feed_all(data) == [proto.TOO_LONG, proto.TOO_LONG, b"PING"]


def test_overlong_line_across_chunks():
    reader = proto.LineReader()
    assert reader.feed(b"A" * 40) == []
    assert reader.feed(b"A" * 40) == []
    assert reader.feed(b"A" * 40 + b"\nM 1") == [proto.TOO_LONG]
    assert reader.feed(b" 2\n") == [b"M 1 2"]


def test_reset_drops_a_partial_line():
    reader = proto.LineReader()
    assert reader.feed(b"KD 22") == []
    reader.reset()
    assert reader.feed(b"HELLO\n") == [b"HELLO"]


def test_reset_clears_the_discard_state():
    reader = proto.LineReader()
    assert reader.feed(b"X" * 80) == []
    reader.reset()
    assert reader.feed(b"HELLO\n") == [b"HELLO"]


# --- parse ---

@pytest.mark.parametrize("line, cmd, args", [
    (b"HELLO", "HELLO", ()),
    (b"PING", "PING", ()),
    (b"R", "R", ()),
    (b"M 0 0", "M", (0, 0)),
    (b"M 32767 32767", "M", (32767, 32767)),
    (b"BD 1 100 200", "BD", (1, 100, 200)),
    (b"BU 5 0 32767", "BU", (5, 0, 32767)),
    (b"W -2 0 16384 16384", "W", (-2, 0, 16384, 16384)),
    (b"W 127 -127 0 0", "W", (127, -127, 0, 0)),
    (b"KD 4", "KD", (4,)),
    (b"KD 101", "KD", (101,)),
    (b"KD 224", "KD", (224,)),
    (b"KU 231", "KU", (231,)),
])
def test_valid_commands(line, cmd, args):
    assert proto.parse(line) == (cmd, args, None)


@pytest.mark.parametrize("line, cmd, err", [
    (b"HELLO 1", "HELLO", "bad-args"),
    (b"R 0", "R", "bad-args"),
    (b"M 1", "M", "bad-args"),
    (b"M 1 2 3", "M", "bad-args"),
    (b"M a b", "M", "bad-args"),
    (b"M 1.5 2", "M", "bad-args"),
    (b"M 1  2", "M", "bad-args"),
    (b"M 1 2 ", "M", "bad-args"),
    (b"M -1 0", "M", "bad-args"),
    (b"M 0 32768", "M", "bad-args"),
    (b"BD 1 2", "BD", "bad-args"),
    (b"BD 0 1 1", "BD", "bad-code"),
    (b"BD 6 1 1", "BD", "bad-code"),
    (b"BD 0 99999 0", "BD", "bad-code"),
    (b"BU 1 32768 0", "BU", "bad-args"),
    (b"BU 1 0 -1", "BU", "bad-args"),
    (b"W 1 2 3", "W", "bad-args"),
    (b"W 128 0 0 0", "W", "bad-args"),
    (b"W 0 -128 0 0", "W", "bad-args"),
    (b"W 0 0 32768 0", "W", "bad-args"),
    (b"KD", "KD", "bad-args"),
    (b"KD x", "KD", "bad-args"),
    (b"KD 4 5", "KD", "bad-args"),
    (b"KD 3", "KD", "bad-code"),
    (b"KD 102", "KD", "bad-code"),
    (b"KD 223", "KD", "bad-code"),
    (b"KU 232", "KU", "bad-code"),
    (b"KD 300", "KD", "bad-code"),
    (b"KD -1", "KD", "bad-code"),
])
def test_bad_arguments_and_codes(line, cmd, err):
    assert proto.parse(line) == (cmd, (), err)


@pytest.mark.parametrize("line, cmd", [
    (b"FOO", "FOO"),
    (b"m 1 2", "m"),
    (b"HELO", "HELO"),
    (b" M 1 2", ""),
    (b"\x01X 1", "\x01X"),
])
def test_unknown_commands(line, cmd):
    assert proto.parse(line) == (cmd, (), "unknown")


def test_undecodable_line():
    assert proto.parse(b"\xff\xfe") == ("-", (), "bad-args")


def test_plan_example_session():
    stream = (b"HELLO\r\nM 16384 16384\nBD 1 16384 16384\nBU 1 16384 16384\n"
              b"KD 225\nKD 4\nKU 4\nKU 225\nW -2 0 16384 16384\nKD 300\nM 0 0\n")
    assert [proto.parse(line) for line in feed_all(stream)] == [
        ("HELLO", (), None),
        ("M", (16384, 16384), None),
        ("BD", (1, 16384, 16384), None),
        ("BU", (1, 16384, 16384), None),
        ("KD", (225,), None),
        ("KD", (4,), None),
        ("KU", (4,), None),
        ("KU", (225,), None),
        ("W", (-2, 0, 16384, 16384), None),
        ("KD", (), "bad-code"),
        ("M", (0, 0), None),
    ]


# --- gate ---

def test_always_ok_commands():
    assert set(proto.ALWAYS_OK) == {"HELLO", "PING", "R"}


@pytest.mark.parametrize("cmd", proto.ALWAYS_OK)
@pytest.mark.parametrize("session", [False, True])
@pytest.mark.parametrize("halted", [False, True])
def test_always_ok_commands_pass_the_gate(cmd, session, halted):
    assert proto.gate(cmd, session, halted) is None


@pytest.mark.parametrize("cmd", INPUT_CMDS)
def test_input_commands_need_a_session(cmd):
    assert proto.gate(cmd, True, False) is None
    assert proto.gate(cmd, False, False) == "no-session"


@pytest.mark.parametrize("cmd", INPUT_CMDS)
def test_halted_is_checked_before_the_session(cmd):
    assert proto.gate(cmd, True, True) == "halted"
    assert proto.gate(cmd, False, True) == "halted"


# --- kbd_code ---

@pytest.mark.parametrize("usage, code", [
    (4, 4), (41, 41), (101, 101),
    (224, -1), (225, -2), (226, -4), (227, -8),
    (228, -16), (229, -32), (230, -64), (231, -128),
])
def test_kbd_code(usage, code):
    assert proto.kbd_code(usage) == code


def test_kbd_codes_are_distinct():
    usages = list(range(4, 102)) + list(range(224, 232))
    codes = [proto.kbd_code(u) for u in usages]
    assert len(set(codes)) == len(codes)
    assert all(c > 0 for c in codes[:98])
    assert all(c < 0 for c in codes[98:])


# --- err_line ---

@pytest.mark.parametrize("cmd, code, expected", [
    ("KD", "bad-code", b"ERR KD bad-code\n"),
    ("M", "halted", b"ERR M halted\n"),
    ("BD", "no-session", b"ERR BD no-session\n"),
    ("FOO", "unknown", b"ERR FOO unknown\n"),
    ("~!", "unknown", b"ERR ~! unknown\n"),
    ("-", "too-long", b"ERR - too-long\n"),
    ("-", "internal", b"ERR - internal\n"),
    ("", "unknown", b"ERR - unknown\n"),
    ("\x01X", "unknown", b"ERR - unknown\n"),
    ("A B", "unknown", b"ERR - unknown\n"),
    ("\tM", "unknown", b"ERR - unknown\n"),
    ("\x7f", "unknown", b"ERR - unknown\n"),
    ("é", "unknown", b"ERR - unknown\n"),
])
def test_err_line(cmd, code, expected):
    assert proto.err_line(cmd, code) == expected


@pytest.mark.parametrize("line", [
    b"", b"FOO", b" M 1 2", b"\x01X 1", b"\tPING", b"\xff\xfe", b"\xc3\xa9", b"M 1", b"KD 300", b"BD 9 1 1",
])
def test_error_replies_are_one_line_of_three_ascii_words(line):
    cmd, args, err = proto.parse(line)
    assert err is not None
    text = proto.err_line(cmd, err).decode("ascii")
    assert text.endswith("\n") and text.count("\n") == 1
    parts = text[:-1].split(" ")
    assert len(parts) == 3
    assert parts[0] == "ERR" and parts[1] and parts[2] == err

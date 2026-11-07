# Line framing and command parsing for the data serial port (protocol version 1).
# Pure Python: runs on the board and under CPython for tests.

PROTO_VERSION = 1
MAX_LINE = 48  # bytes, excluding the newline

TOO_LONG = object()  # yielded by LineReader in place of an overlong line

HID_MAX = 32767

# Accepted whatever the session and HALTED state, so the server can always
# re-arm (HELLO), check liveness (PING) and release everything (R).
ALWAYS_OK = ("HELLO", "PING", "R")

# command -> argument count
_ARGC = {
    "HELLO": 0,
    "PING": 0,
    "R": 0,
    "M": 2,
    "BD": 3,
    "BU": 3,
    "W": 4,
    "KD": 1,
    "KU": 1,
}


class LineReader:
    # Splits incoming bytes into lines. "\r" is ignored. A line longer than
    # MAX_LINE is discarded up to the next newline and reported once as TOO_LONG.

    def __init__(self):
        self.reset()

    def reset(self):
        # Forgets a partial line, e.g. one cut off when the previous server closed the port.
        self._buf = bytearray()
        self._discard = False

    def feed(self, data):
        lines = []
        for b in data:
            if b == 10:  # \n
                if self._discard:
                    self._discard = False
                    lines.append(TOO_LONG)
                elif self._buf:
                    lines.append(bytes(self._buf))
                self._buf = bytearray()
            elif b == 13 or self._discard:  # \r, or inside an overlong line
                continue
            elif len(self._buf) >= MAX_LINE:
                self._discard = True
                self._buf = bytearray()
            else:
                self._buf.append(b)
        return lines


def _key_ok(u):
    return 4 <= u <= 101 or 224 <= u <= 231


def parse(line):
    # Returns (cmd, args, err). err is None on success, otherwise one of
    # "unknown", "bad-args", "bad-code". cmd is "-" if the line does not decode;
    # for an unknown command it is the raw first token (err_line cleans it up).
    try:
        parts = line.decode().split(" ")
    except Exception:
        return "-", (), "bad-args"
    cmd = parts[0]
    argc = _ARGC.get(cmd)
    if argc is None:
        return cmd, (), "unknown"
    if len(parts) - 1 != argc:
        return cmd, (), "bad-args"
    try:
        args = tuple(int(p) for p in parts[1:])
    except ValueError:
        return cmd, (), "bad-args"

    if cmd == "M":
        if not (0 <= args[0] <= HID_MAX and 0 <= args[1] <= HID_MAX):
            return cmd, (), "bad-args"
    elif cmd in ("BD", "BU"):
        if not 1 <= args[0] <= 5:
            return cmd, (), "bad-code"
        if not (0 <= args[1] <= HID_MAX and 0 <= args[2] <= HID_MAX):
            return cmd, (), "bad-args"
    elif cmd == "W":
        if not (-127 <= args[0] <= 127 and -127 <= args[1] <= 127):
            return cmd, (), "bad-args"
        if not (0 <= args[2] <= HID_MAX and 0 <= args[3] <= HID_MAX):
            return cmd, (), "bad-args"
    elif cmd in ("KD", "KU"):
        if not _key_ok(args[0]):
            return cmd, (), "bad-code"
    return cmd, args, None


def gate(cmd, session, halted):
    # Returns None if a parsed command may run now, else its ERR code. HALTED is
    # checked first: it outlives sessions and only HELLO clears it.
    if cmd in ALWAYS_OK:
        return None
    if halted:
        return "halted"
    if not session:
        return "no-session"
    return None


def kbd_code(usage):
    # keyboard.py takes the modifiers (224-231) as negated bits of the modifier byte.
    if usage >= 224:
        return -(1 << (usage - 224))
    return usage


def _is_word(s):
    for c in s:
        if not 0x21 <= ord(c) <= 0x7E:
            return False
    return len(s) > 0


def err_line(cmd, code):
    # The server splits "ERR <cmd> <code>" on spaces, so the echoed token must be
    # one printable ASCII word.
    if not _is_word(cmd):
        cmd = "-"
    return ("ERR %s %s\n" % (cmd, code)).encode()

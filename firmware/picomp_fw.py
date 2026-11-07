# PiComp board firmware: composite USB device (REPL CDC + data CDC + keyboard +
# absolute mouse) driven by text commands on the data port. PLAN.md sections 3
# (firmware) and 4 (protocol) describe the behaviour.
#
# The held keys, buttons and X/Y are the desired state. A report the host did not
# take leaves its interface dirty, and the loop resends it until the host does.
#
# Try it from RAM first:  python -m mpremote connect COM6 exec "import picomp_fw; picomp_fw.run()"
# From M5 on, main.py runs it at power-up.
import time

import rp2
import usb.device
from usb.device.cdc import CDCInterface
from usb.device.keyboard import KeyboardInterface

import proto
from absmouse import AbsMouse
from led import Led

FW_VERSION = "0.1.0"

BLUE = (0, 0, 20)  # USB up, no session
GREEN = (0, 20, 0)  # session open, nothing held
ORANGE = (20, 8, 0)  # something held
RED = (20, 0, 0)  # HALTED, or an internal error since the last HELLO

_PAN_SIGN = 1  # flip to -1 if M3a shows horizontal scrolling reversed
_HEARTBEAT_MS = 1000
_BOOT_POLL_MS = 50
_SEND_TIMEOUT_MS = 20
_KEY_SLOTS = 6  # non-modifier keys in one keyboard report
_DRAIN_CHUNKS = 16  # bounds the discard on a DTR drop in case a host keeps writing

_HELLO_REPLY = ("OK PICOMP %d %s\n" % (proto.PROTO_VERSION, FW_VERSION)).encode()
_PONG = b"PONG\n"
_HALT = b"!HALT\n"
_TIMEOUT = b"!TIMEOUT\n"
_ERR_TOO_LONG = proto.err_line("-", "too-long")
_ERR_INTERNAL = proto.err_line("-", "internal")


class Firmware:
    def __init__(self):
        self.led = Led()
        self.led.set(*BLUE)

        self.cdc = CDCInterface(timeout=0)  # non-blocking reads and writes
        self.kbd = KeyboardInterface()
        self.kbd.interval_ms = 1
        self.mouse = AbsMouse()

        self.reader = proto.LineReader()
        self.rx = bytearray(64)
        self.rx_mv = memoryview(self.rx)

        self.session = False
        self.halted = False
        self.error = False
        self.held = []  # proto.kbd_code values, in press order
        self.buttons = 0  # bit b-1 is set while button b is down
        self.x = 0
        self.y = 0
        self.sent_buttons = 0  # buttons in the last mouse report the host took
        self.kbd_dirty = False
        self.mouse_dirty = False
        self.last_rx = time.ticks_ms()
        self.last_dtr = False
        self.boot_prev = rp2.bootsel_button()
        self.next_boot = time.ticks_ms()

        self.commands = {
            "HELLO": self.cmd_hello,
            "PING": self.cmd_ping,
            "R": self.release_all,
            "M": self.cmd_move,
            "BD": self.cmd_button_down,
            "BU": self.cmd_button_up,
            "W": self.cmd_wheel,
            "KD": self.cmd_key_down,
            "KU": self.cmd_key_up,
        }

    def init_usb(self):
        # Data CDC first so it always lands on interfaces 2-3 (REPL CDC keeps 0-1).
        # Class 0xEF/02/01 tells Windows to honour the CDCs' interface association descriptors.
        usb.device.get().init(
            self.cdc,
            self.kbd,
            self.mouse,
            builtin_driver=True,
            device_class=0xEF,
            device_subclass=2,
            device_protocol=1,
        )

    def reply(self, data):
        # Only while the server has the port open, and only whole lines: a partial
        # write would corrupt the next line the server reads.
        # (_wb is the vendored CDC library's transmit buffer.)
        if self.cdc.dtr and self.cdc._wb.writable() >= len(data):
            self.cdc.write(data)

    def _send_kbd(self, cmd):
        self.kbd_dirty = True  # stays set if the send raises, so the loop retries it
        if self.kbd.send_keys(self.held, timeout_ms=_SEND_TIMEOUT_MS):
            self.kbd_dirty = False
        elif cmd:
            self.reply(proto.err_line(cmd, "usb-busy"))

    def _send_mouse(self, cmd, wheel=0, pan=0):
        self.mouse_dirty = True
        if self.mouse.send(self.buttons, self.x, self.y, wheel, pan):
            self.mouse_dirty = False
            self.sent_buttons = self.buttons
        elif cmd:
            self.reply(proto.err_line(cmd, "usb-busy"))

    def release_all(self):
        self.held = []
        self.buttons = 0
        self._send_kbd(None)  # an all-up keyboard report has no visible effect
        # The mouse report goes to the last X/Y, so skip it when nothing can be
        # stuck: an idle board never moves the cursor.
        if self.sent_buttons or self.mouse_dirty:
            self._send_mouse(None)

    def cmd_hello(self):
        self.release_all()
        self.session = True
        self.halted = False
        self.error = False
        self.reply(_HELLO_REPLY)

    def cmd_ping(self):
        self.reply(_PONG)

    def cmd_move(self, x, y):
        self.x, self.y = x, y
        self._send_mouse("M")

    def cmd_button_down(self, b, x, y):
        self.buttons |= 1 << (b - 1)
        self.x, self.y = x, y
        self._send_mouse("BD")

    def cmd_button_up(self, b, x, y):
        self.buttons &= ~(1 << (b - 1))
        self.x, self.y = x, y
        self._send_mouse("BU")

    def cmd_wheel(self, v, h, x, y):
        # If this report fails its notches are lost: the dirty resend carries no wheel motion.
        self.x, self.y = x, y
        self._send_mouse("W", v, _PAN_SIGN * h)

    def cmd_key_down(self, usage):
        c = proto.kbd_code(usage)
        if c in self.held:
            return
        if c > 0 and sum(1 for k in self.held if k > 0) >= _KEY_SLOTS:
            # keyboard.py would turn a 7th key into a rollover-error report
            self.reply(proto.err_line("KD", "bad-args"))
            return
        self.held.append(c)
        self._send_kbd("KD")

    def cmd_key_up(self, usage):
        c = proto.kbd_code(usage)
        if c in self.held:
            self.held.remove(c)
            self._send_kbd("KU")

    def handle(self, line):
        if line is proto.TOO_LONG:
            self.reply(_ERR_TOO_LONG)
            return
        cmd, args, err = proto.parse(line)
        if not err:
            err = proto.gate(cmd, self.session, self.halted)
        if err:
            self.reply(proto.err_line(cmd, err))
        else:
            self.commands[cmd](*args)

    def link_edge(self, up):
        # Either edge ends the session and forgets a half-received line. A drop also
        # discards what the closed connection left queued; a rise does not, because
        # the new server's HELLO may already be queued.
        self.last_dtr = up
        self.session = False
        self.release_all()
        self.reader.reset()
        if not up:
            for _ in range(_DRAIN_CHUNKS):
                if not self.cdc.readinto(self.rx):
                    break

    def check_heartbeat(self, now):
        # A server that stops sending (hung, or gone without DTR dropping) must not
        # leave anything held for more than a second.
        if (self.held or self.buttons) and time.ticks_diff(now, self.last_rx) > _HEARTBEAT_MS:
            self.release_all()
            self.reply(_TIMEOUT)

    def check_boot(self, now):
        if time.ticks_diff(now, self.next_boot) < 0:
            return
        self.next_boot = time.ticks_add(now, _BOOT_POLL_MS)
        pressed = rp2.bootsel_button()
        if pressed and not self.boot_prev:  # press edge only: holding BOOT halts once
            self.release_all()
            self.halted = True
            self.reply(_HALT)
        self.boot_prev = pressed

    def resend_dirty(self):
        # Only when the endpoint is free, so a host that is not polling never stalls the loop.
        if self.kbd_dirty and self.kbd.is_open() and not self.kbd.busy():
            self._send_kbd(None)
        if self.mouse_dirty and self.mouse.is_open() and not self.mouse.busy():
            self._send_mouse(None)

    def show_state(self):
        if self.halted or self.error:
            self.led.set(*RED)
        elif self.held or self.buttons:
            self.led.set(*ORANGE)
        elif self.session:
            self.led.set(*GREEN)
        else:
            self.led.set(*BLUE)

    def step(self):
        now = time.ticks_ms()
        # Read before sampling DTR: a new connection's bytes only arrive after its
        # DTR rise, so whatever is read here is never handled under the old session.
        n = self.cdc.readinto(self.rx)
        # is_open() catches a bus reset, which leaves the last DTR value latched.
        up = self.cdc.dtr and self.cdc.is_open()
        if up != self.last_dtr:
            self.link_edge(up)
            if not up:
                n = None  # sent before the port closed: discarded with the rest
        if n:
            for line in self.reader.feed(self.rx_mv[:n]):
                self.last_rx = now
                self.handle(line)
        self.check_heartbeat(now)
        self.check_boot(now)
        self.resend_dirty()
        self.show_state()

    def recover(self):
        # step() raised. Nothing here may raise again: that would end the loop.
        self.error = True
        try:
            self.release_all()
        except Exception:
            pass
        try:
            self.reply(_ERR_INTERNAL)
        except Exception:
            pass

    def run(self):
        self.init_usb()
        try:
            while True:
                try:
                    self.step()
                except Exception:  # never BaseException: mpremote's Ctrl-C must still stop us
                    self.recover()
                time.sleep_ms(1)  # lets the USB stack run
        finally:
            self.release_all()  # also on mpremote's Ctrl-C, which still propagates
            self.led.off()  # dark LED: the firmware is not running


def run():
    Firmware().run()

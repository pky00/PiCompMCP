# PiCompMCP v1 build plan

## 1. Overview

v1 **records** what you do with your own mouse and keyboard on this laptop. It then **replays** that recording once, blind, through the RP2350 board, which acts as a real USB keyboard and absolute mouse. One Python process (conda env `picomp`) owns four things: the board's data serial port, the input hooks, SQLite and a small browser GUI. v1 has no MCP and no LLM. Every action goes through one `Controller` class, so MCP tools can later call the same methods without rework.

> **Recording is done in software, not through the board's USB-A port.** Windows low-level hooks (pynput) on the laptop capture your real mouse and keyboard, which stay plugged into the laptop (the hub is on a laptop port). The board's USB-A port stays empty in v1: nothing plugged into it would work. Waveshare's FAQ says MicroPython supports only USB device mode, not host mode.
>
> Recording through that port would need two things:
> - C/C++ firmware with TinyUSB host and Pico-PIO-USB.
> - Removing resistor R13. The same FAQ says R13 must be removed for hot-plugging and for low-speed devices, which covers most keyboards and mice. After that, the port can no longer act as a device.
>
> That is a v2 item. The software route also records the laptop's own keyboard and the touchpad's pointer and clicks. Whether two-finger touchpad scrolling is captured is the first check in M7.

```
 you: keyboard + mouse (hub on a LAPTOP port), touchpad       browser tab (GUI)
                 |                                                  | HTTP 127.0.0.1:8765
                 v                                                  | (token cookie + Origin check)
  +------- Windows 11 input stack --------+                         v
  |  HID drivers -> cursor / focused app  |        +----------------------------+
  |  low-level hooks (see you AND board) --------> | python -m picomp           |
  +----------------^----------------------+        |  web -> controller         |
                   | HID reports                   |  hooks -> recorder/player  |
                   |                               |  db (SQLite)  win (Win32,  |
                   |                               |  board (pyserial)   mss)   |
                   |                               +-------------+--------------+
                   |                                             | data CDC (COMx)
  +----------------+---------------------------------------------+------------+
  | RP2350-USB-A on USB-C (native USB), MicroPython v1.29.0, firmware main.py |
  |  itf 0-1 REPL CDC (built in: mpremote)   itf 2-3 data CDC (commands)      |
  |  itf 4   HID keyboard                    itf 5   HID absolute mouse       |
  |  GPIO16 LED (PIO WS2812)   BOOT = kill   USB-A port: unused (no host)     |
  +---------------------------------------------------------------------------+
```

## 2. Repo layout and environment

```
PiCompMCP/
  environment.yml        conda env "picomp" (new deps via pip only)
  README.md              flash, install firmware, run server, recovery, limitations
  .gitignore             data/
  firmware/              MicroPython; everything except dev scripts is copied to the board
    main.py              import picomp_fw; picomp_fw.run()      (installed in M5)
    picomp_fw.py         composite USB init, main loop, safety, LED
    proto.py             pure line parser (runs on MicroPython and CPython for pytest)
    absmouse.py          AbsMouse(HIDInterface): 7-byte absolute report
    led.py               PIO WS2812 driver (lifted from led_test.py)
    led_test.py, wiggle.py   existing bring-up scripts (dev only, not copied)
    usb/README.md        provenance; now says "copied to the board", not "mount"
    usb/device/          vendored micropython-lib, unmodified
  picomp/                server package, run with: python -m picomp
    __main__.py  controller.py  board.py  win.py  keymap.py
    hooks.py  recorder.py  player.py  db.py  web.py
    static/index.html    the GUI (HTML + inline JS/CSS)
  tools/fwcheck.py       CLI that drives the board directly (bring-up)
  tests/                 pytest, pure logic, no hardware
  data/picomp.db         SQLite (git-ignored)
```

`environment.yml` (apply with `conda env update -n picomp -f environment.yml`):

```yaml
name: picomp
dependencies:          # no channels line: keeps the env's existing python/sqlite builds
  - python=3.12
  - pip
  - pip:
      - pyserial==3.5
      - fastapi==0.143.0
      - uvicorn>=0.31
      - pynput==1.8.2
      - mss==10.2.0
      - mpremote>=1.29
      - pytest>=8
      - httpx>=0.27        # FastAPI TestClient
```

After M6, replace the `>=` entries with the exact versions from `python -m pip freeze`.

**Vendored libraries.** Add `usb-device-keyboard/usb/device/keyboard.py` (v0.1.1) and `usb-device-cdc/usb/device/cdc.py` (v0.1.4), unmodified, from micropython-lib commit `f652fb2dfdd231e9c9c0cc1a10e12fdc9025ce95`. They sit next to the existing `__init__.py`, `core.py`, `hid.py` and `mouse.py`. `mouse.py` stays because `wiggle.py` uses it.

**Copying to the board.** Copy the files onto the 1.4 MB filesystem with mpremote. `mount` does not work here: it is not there at power-up and it drops on every re-enumeration. Always run `python -m mpremote`. Once `main.py` exists, always add `resume`, so mpremote does not soft-reset the board and drop the USB device.

```
python -m mpremote connect COM6 cp -r firmware/usb :
python -m mpremote connect COM6 resume cp firmware/led.py firmware/proto.py firmware/absmouse.py firmware/picomp_fw.py :
python -m mpremote connect COM6 resume cp firmware/main.py :main.py     # from M5 on
python -m mpremote connect COM6 reset
```

## 3. Firmware

**USB composition.** All interfaces are set up in one call in `picomp_fw.run()`:

```python
cdc = CDCInterface(timeout=0)                  # non-blocking data port
kbd = KeyboardInterface(); kbd.interval_ms = 1
mouse = AbsMouse()                             # interval_ms=1
usb.device.get().init(cdc, kbd, mouse, builtin_driver=True,
                      device_class=0xEF, device_subclass=2, device_protocol=1)
```

- **Interface numbers.** The data CDC is passed first, so it is always interfaces 2-3; pyserial reports its location ending in `:x.2`. The REPL CDC keeps interfaces 0-1, the keyboard is 4 and the mouse is 5. That is 6 of the 16 interfaces available, and well within the endpoint memory.
- **Device class.** It must be 0xEF/02/01. Without it, Windows ignores the interface association descriptors that both CDCs use.
- **Polling interval.** 1 ms, so each `send_report` waits about 1 ms at most.

**Where init runs.** Init lives in `main.py`, and there is no `boot.py`. If init raised an exception inside `boot.py`, the board would have no USB at all, and rp2 has no safe mode. With init in `main.py`, the REPL CDC survives an init failure. Two rules keep the board recoverable:

1. Prove `picomp_fw` by running it from RAM (`exec "import picomp_fw; picomp_fw.run()"`) before any `main.py` exists. If that fails, pressing RESET recovers.
2. Catch `Exception` in each loop iteration, never `BaseException`. The Ctrl-C that mpremote sends then still stops the program.

Recovery, in order:

1. `python -m mpremote connect <REPL port> resume rm :main.py`, then `reset`.
2. Hold BOOT while plugging in. Flash the RP2350 flash-nuke UF2, then the SEEED_XIAO_RP2350 v1.29.0 UF2, then redo the copy commands. Re-flashing alone keeps `main.py`.

**Absolute mouse.** `AbsMouse` subclasses `HIDInterface(DESC, protocol=0, interval_ms=1)`. The report is `struct "<BHHbb"`: buttons, X, Y, wheel, pan. That is 7 bytes, under the 8-byte endpoint limit. The descriptor is TinyUSB's `TUD_HID_REPORT_DESC_ABSMOUSE`, the same layout PiKVM and QEMU use:

```
05 01 09 02 A1 01 09 01 A1 00       Generic Desktop / Mouse / App coll. / Pointer / Phys coll.
05 09 19 01 29 05 15 00 25 01 95 05 75 01 81 02   5 buttons (L R M back fwd)
95 01 75 03 81 01                   3 padding bits
05 01 09 30 09 31 15 00 26 FF 7F 75 10 95 02 81 02   X, Y: 0..32767, 16-bit LE, Abs
09 38 15 81 25 7F 95 01 75 08 81 06                  Wheel -127..127 Rel (1 = one notch)
05 0C 0A 38 02 15 81 25 7F 95 01 75 08 81 06         AC Pan -127..127 Rel (horizontal)
C0 C0
```

- **Protocol fix.** `AbsMouse.desc_cfg` sets `self.protocol = 0` before calling `super()`. Upstream `hid.py` leaves `protocol = 1` after its sizing pass, which would advertise the mouse as a boot keyboard. The fix lives in our subclass, so the vendored file stays unmodified.
- **Sending.** `send()` waits until the endpoint is not busy (20 ms deadline), runs `pack_into` on a preallocated buffer, then calls `send_report`.

**Keyboard.** The vendored `KeyboardInterface` is used as it is.

- The server sends HID usages: keys 4-101, and modifiers 0xE0-0xE7. The firmware converts modifiers to keyboard.py's `-(1 << (code - 0xE0))`.
- The firmware keeps a `held` list and calls `kbd.send_keys(held, timeout_ms=20)` on every change.
- Limits: 6 keys plus 8 modifiers. F13-F24 and media keys cannot be sent.

**Report delivery.** The held keys, buttons and X/Y are the *desired* state.

- A send that returns False means the host is not polling, for example during suspend or re-enumeration. The firmware then replies `ERR <cmd> usb-busy` and marks that interface dirty.
- The loop resends a dirty interface's current state until a send succeeds. State is never treated as delivered just because a send was attempted.
- `release_all()` clears held keys and buttons, then:
  - always sends an all-up keyboard report, which has no visible effect;
  - sends a mouse report with buttons at 0 at the last X/Y, but only if the last successful mouse report had a button down or the mouse is dirty. An idle board therefore never moves the cursor.

**Session and safety.**

- **Session.** `HELLO` starts a session, and DTR dropping (or rising) ends it. `M BD BU W KD KU` sent outside a session get `ERR <cmd> no-session`. A stray program that opens the port cannot press keys.
- **DTR drop.** Calls `release_all()`.
- **Heartbeat.** If anything is held and no line has arrived for 1000 ms, the firmware calls `release_all()` and replies `!TIMEOUT`.
- **BOOT kill.** This applies only if M1 passes.
  - The button is polled every 50 ms. A press (an edge from released to pressed) calls `release_all()`, sets HALTED and replies `!HALT`.
  - While halted, `M BD BU W KD KU` get `ERR <cmd> halted`.
  - `HELLO`, `PING` and `R` are always accepted. `R` is a successful no-op while halted, and `HELLO` clears HALTED.
  - If M1 fails, drop the BOOT kill. The kill hotkey, the GUI Stop button and unplugging remain.

**Main loop** (one plain loop, no asyncio):

```python
def run():
    init_usb()
    try:
        while True:
            try:
                step()   # readinto 64 B chunk -> lines -> execute; DTR / heartbeat / BOOT checks;
                         # resend dirty interfaces; LED
            except Exception:
                error = True; release_all(); reply(b"ERR - internal\n")
            time.sleep_ms(1)   # services USB (all USB work runs through the scheduler)
    finally:
        release_all()          # also runs on mpremote's Ctrl-C; KeyboardInterrupt still propagates
```

- Replies are written only while `cdc.dtr` is set, and partial writes are dropped.
- There is no `print` in the loop.

**LED** (brightness about 20/255):

| colour | meaning |
|---|---|
| blue | USB up, no session |
| green | session open, nothing held |
| orange | something held |
| red | HALTED, or an internal error since the last HELLO |

## 4. Serial protocol (data CDC, protocol version 1)

**Framing.** ASCII lines ending in `\n` (`\r` is ignored), at most 48 bytes, with decimal integers separated by single spaces. The busiest traffic is about 60 moves per second at about 20 bytes each, so a binary format would save nothing. An overlong line is discarded up to the next newline, with the reply `ERR - too-long`.

| command (server to board) | meaning | reply |
|---|---|---|
| `HELLO` | start the session, release everything, clear HALTED | `OK PICOMP 1 <fw_version>` |
| `PING` | heartbeat, every 250 ms | `PONG` |
| `M x y` | absolute move | none |
| `BD b x y` / `BU b x y` | button down/up at x,y; b: 1 left, 2 right, 3 middle, 4 back, 5 forward | none |
| `W v h x y` | wheel notches at x,y; v: + is up, h: + is right; range -127..127 | none |
| `KD u` / `KU u` | key down/up; HID usage 4-101 or 224-231 | none |
| `R` | release all keys and buttons | none |

- **Board to server:** `ERR <CMD> <bad-args|bad-code|halted|no-session|usb-busy|unknown|internal>`, `!HALT`, `!TIMEOUT`.
- **Coordinates:** always HID units from 0 to 32767; pixels never cross the wire. Every mouse command carries x,y. The conversion is in `win.py`: `hid = clamp(round(p * 32767 / (W - 1)), 0, 32767)`, where W and H are physical pixels from `GetSystemMetrics`. This is exact at the edges and at most 1 px off in between, which does not matter for replay. The M3a corners test confirms the mapping.
- **Handshake:** after opening the port, the server flushes input and sends `HELLO`. It then reads lines for up to 1 s, skipping `PONG`, `!` and `ERR` lines left over from an earlier session, until a line starts with `OK PICOMP`. It rejects the port only if that line names another protocol version or if time runs out.
- **Heartbeat:** the server's reader loop sends `PING` every 250 ms; while a replay is running, only if the dispatcher and replay threads have updated their liveness stamps within the last 1 s. A hung server therefore stops pinging, and the board releases everything after 1 s. If no `PONG` arrives for 1500 ms, the server treats the board as lost and reconnects.

```
> HELLO                 < OK PICOMP 1 0.1.0
> M 16384 16384         (cursor to screen centre)
> BD 1 16384 16384      > BU 1 16384 16384        (left click there)
> KD 225  > KD 4  > KU 4  > KU 225                (Shift+a -> "A")
> W -2 0 16384 16384    (two notches down)
> KD 300                < ERR KD bad-code
  (BOOT pressed)        < !HALT
> M 0 0                 < ERR M halted
> HELLO                 < OK PICOMP 1 0.1.0       (re-armed; sent at each replay start)
```

## 5. Server

| module | responsibility |
|---|---|
| `__main__.py` | DPI awareness first; create the token; start uvicorn on 127.0.0.1:8765; open `/auth?t=<token>` in the browser once the server has started; print that URL |
| `controller.py` | **The core**: the state machine. Every action the GUI, the hotkeys or REST (and later MCP) can take is a method here: `status, start_recording, stop_recording, list/rename/delete_recording, start_replay, resume_replay, stop_replay, kill, get/set_settings, shutdown` |
| `board.py` | BoardLink: port discovery, handshake, reader loop with PING, `send(line)` under a write lock, reconnect; also the pure format and parse helpers |
| `win.py` | Win32 calls through ctypes: DPI setup; screen size, monitor count and DPI; pixel to HID; `GetCursorPos`; foreground window and refocus; `GetAsyncKeyState`; SendInput key/button-up fallback; mss screenshot to PNG |
| `keymap.py` | (scan code, extended flag) to and from HID usage, US layout |
| `hooks.py` | the two pynput listeners; the filter enqueues events and suppresses the Esc of Ctrl+Alt+Esc and the R of Ctrl+Alt+R; `restart()` |
| `recorder.py` | events to steps, the stop cut, in-memory steps and screenshots |
| `player.py` | replay thread: timing, sending, touch detection, echo check, pause, resume and stop |
| `db.py` | schema, connection helper, queries |
| `web.py` | FastAPI app, auth, Origin check, REST routes, serves `static/index.html` |

**Serial client.**

- **Discovery:** filter `comports()` for vid 0x2E8A, pid 0x0005 and `location.endswith(":x.2")`, then do the handshake. Never trust a COM number, and never pick the REPL port (`:x.0`).
- **Port settings:** `timeout=0.1, write_timeout=1`, DTR on.
- **Reading:** the reader loop appends `ser.read(ser.in_waiting or 1)` to a buffer and acts only on complete `\n`-terminated lines. pyserial's `readline()` returns half a line when it times out, so it is not used.
- **Disconnect:** on `SerialException` the reader closes the port, marks the board offline, tells the controller (which stops any replay) and retries discovery every 1 s.

**DPI.** These are the first lines of `__main__.py`, before pynput, mss or uvicorn are imported:

```python
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Per-Monitor v2
# abort unless GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext()) == 2
```

After this, hook points, `GetCursorPos`, `GetSystemMetrics` (1920x1080) and mss all use physical pixels.

**Startup.** uvicorn runs lifespan startup before it binds the socket, so the browser opens only after `server.started`:

```python
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765))  # no reload, no workers
threading.Thread(target=open_browser_when_started, args=(server,), daemon=True).start()
server.run()   # Ctrl+C -> lifespan shutdown
```

**Threads.** uvicorn serves HTTP. Endpoints are plain `def`, so they run in FastAPI's threadpool. The other threads are:

- 2 pynput hook threads. The filter only enqueues events (and suppresses two hotkey keys, described below).
- 1 dispatcher. It drains the queue with a 250 ms timeout and routes events by state. On each 250 ms tick it also updates its liveness stamp, reconciles held keys and buttons, and checks the monitor count.
- 1 screenshot worker.
- 1 board reader.
- 1 replay thread per run.

**Lock rules** (one controller `RLock`):

1. The stop path (kill, auto-pause, `!HALT`, board lost) first sets the player's flag and sends `R` through BoardLink. Only then does it take the controller lock to update state.
2. Never hold the controller lock during SQLite work, serial writes or waits on other threads.
3. Never call into the controller while holding the board write lock. The reader passes `!HALT` and `ERR` on after releasing the lock.

SQLite uses one short-lived connection per operation.

**Auth.**

- **Token:** a new `secrets.token_urlsafe(32)` on every start.
- **Cookie:** `GET /auth?t=` compares the token with `secrets.compare_digest`, sets the cookie `picomp=<token>; HttpOnly; SameSite=Strict; Path=/` and redirects to `/`. Every other route returns 401 without a valid cookie.
- **Origin check:** every non-GET request must carry `Origin: http://127.0.0.1:8765` (or `http://localhost:8765`), otherwise it gets 403. This is needed because a cookie's site ignores the port. A page on any other localhost port counts as same-site, so it would send the cookie, and its body-less POSTs need no preflight. Browsers always send `Origin` on non-GET fetches. GET routes only read data, and with no CORS headers another origin cannot read the responses.
- **Binding:** 127.0.0.1 only, plus `TrustedHostMiddleware(["127.0.0.1", "localhost"])` against DNS rebinding. There is no CORS middleware.

## 6. Recording

**Why pynput 1.8.2.** It runs each low-level hook on its own thread with its own message loop. Its `win32_event_filter(msg, data)` exposes the raw `KBDLLHOOKSTRUCT`/`MSLLHOOKSTRUCT`: scan code, vkCode, the extended and injected flags, the physical point, and wheel and X-button data. Recording scan codes makes recording the exact inverse of what the board sends. The alternatives were rejected:

- `keyboard` and `mouse` (boppreh) are unmaintained.
- Raw ctypes hooks would mean rewriting pynput's thread and message pump.
- Raw Input gives relative deltas, not screen positions.

**The hook filter.**

- It enqueues `(perf_counter_ns(), msg, fields)` and returns `False`, so pynput skips its own callbacks. Events are not suppressed.
- Exception, the hotkeys: on an Esc (kill) or R (record toggle) key-down, the filter checks `GetAsyncKeyState(VK_CONTROL)` and `(VK_MENU)`. These cover both sides and already reflect ups the hooks missed (Ctrl+Alt+Del, Win+L). If both are down, it enqueues the event tagged as a hotkey, suppresses it with `listener.suppress_event()` and flags the key. A flagged key's further downs (auto-repeat) and its up are suppressed and not enqueued, whatever Ctrl and Alt do; the up clears the flag. The foreground app sees the Ctrl and Alt but never the Esc or R, and the dispatcher hands tagged events only to the hotkey logic.

**Record hotkey Ctrl+Alt+R** (dispatcher). The same chord starts and stops a recording; the GUI only shows the state.

- In `idle` it calls `start_recording`, which requires exactly one monitor (the board is not needed), with `t0` = the R key-down's hook time. A refusal shows its reason in the GUI status line.
- In `recording` it calls `stop_recording` (cut and save, below).
- In any other state it does nothing itself. During a replay the chord's Ctrl and Alt are physical touches, so auto-pause handles them like any other touch. A replay never sends the chord, because recordings never contain it.

**Event to step rules** (dispatcher thread, state = recording):

- **Injected events:** events flagged `LLMHF_INJECTED|LOWER_IL` or `LLKHF_INJECTED|LOWER_IL` are dropped and counted. The result after stop reports "N software-injected inputs ignored", which points to remapping or automation tools.
- **Time:** `t_ms = (event time - t0) / 1e6`.
- **Moves:** the point is clamped to the screen. The newest move is held back as pending. It is written once 16 ms have passed since the last written move, or before any other step, or at stop. That gives about 60 Hz and keeps the exact final position.
- **Buttons:** `WM_[LRM]BUTTON*` and `WM_XBUTTON*` become `bdown`/`bup` with a code from 1 to 5 and x,y. A `bup` with no recorded `bdown` is dropped.
- **Wheel:** the signed `mouseData` deltas are summed per axis. Each whole multiple of 120 becomes one `wheel` step, and the remainder is carried over.
- **Keys:** (scan, extended) maps to a HID usage.
  - The fake-shift codes (0x2A, extended) and (0x36, extended) are dropped explicitly.
  - Unmapped keys (media, volume, the Fn layer) are dropped and logged.
  - A repeated key-down for a key already down (Windows auto-repeat) is dropped. A key-up with no matching key-down is dropped.
- **Missed key-ups:** on each 250 ms tick, any key or button the recorder has held for more than 250 ms is checked with `GetAsyncKeyState`. Keys use their vkCode. Mouse buttons use VK_LBUTTON and VK_RBUTTON, swapped if `SM_SWAPBUTTON` is set. If Windows reports the key or button up, the recorder inserts a `kup`/`bup` at that time. This covers key-ups the hooks never saw: a UAC prompt, Win+L, or Alt+Tab into an admin window. The dispatcher's own physically-held model, used for touch detection, is reconciled the same way.
- **Display change:** if the monitor count, screen size or DPI changes during a recording, stop discards the recording and the GUI shows that reason.

**Cutting the chords out.**

- **Start:** only events after `t0` are kept. Anything still held at `t0` (Ctrl, Alt, a mouse button) gives orphan ups later, which are dropped like any up without a recorded down.
- **Stop:** steps are cut from the last recorded key or button down, before the stop chord's R, that started with no recorded key or button held. That removes the chord's Ctrl and Alt; if there is no such down, nothing is cut. Pure moves after the last remaining non-move step are then removed. Anything held when the chord starts (a drag, or Ctrl kept down after Ctrl+clicks) is cut back to its press, so release everything first.
- **Ctrl+C while recording:** the lifespan shutdown saves the recording through `stop_recording`, cut the same way back from the shutdown time, which removes the Ctrl+C. A server crash loses the recording in progress.

**Screenshots.** The setting is `each_click` (default), `start_end` or `none`. The worker grabs monitor 1, encodes a PNG and keeps the bytes in memory.

- `each_click`: on every `bdown`, the dispatcher passes the step's `seq` to the screenshot worker.
- `start_end`: one grab at `t0` (stored as seq 0) and one at stop (stored as seq n, the final step count).
- A failed grab, for example on the secure desktop, is skipped and logged.

**Writing.** Steps and screenshots stay in memory until stop. At stop the server applies the cut (dropping shots of cut steps), drains the screenshot worker, and then writes the recording row, its steps and its screenshots in one transaction. A recording with no steps left is not saved.

## 7. Replay

**Start** (`POST /api/recordings/{id}/replay`). It requires:

- state `idle`;
- the board online;
- exactly one monitor;
- the recorded screen size and DPI equal to the current ones.

Otherwise it returns 409 with the reason. Then it:

1. Recreates the hook listeners.
2. Sends `HELLO`, which re-arms the board after a BOOT halt and releases everything.
3. Loads the steps, without screenshots.
4. Starts the replay thread.

**Timing loop.**

- Each step's deadline is `t_start + t_ms + shift`. The thread waits with `time.sleep(min(remaining, 0.005))` in a loop, checking the stop and pause flags between slices.
  - On Python 3.12, `time.sleep` is high-resolution. `Event.wait` and lock timeouts follow the 15.6 ms system tick (11-33 ms measured here), so they are never used as timers.
- **Lateness:**
  - More than 10 ms late: log it.
  - More than 100 ms late: add the lateness to `shift`. The remaining steps keep their spacing, and overdue steps do not go out in a burst (two clicks would become a double-click).
  - More than 1 s late: auto-pause with "replay fell behind (system stalled or slept)".
- **Before each step:**
  - If `GetCursorPos` fails, pause with "secure desktop".
  - If the dispatcher's liveness stamp is older than 1 s, pause with "input monitor stalled".
  - Convert pixels to HID, record the expected events, then send.
  - Never send Esc while the replay holds both Ctrl and Alt.
- **After each step:** store `GetForegroundWindow()` as the target window.
- **At the end:** send `R` and go to `idle`.

**Touch detection.** Board output looks like real hardware to the hooks, so the player compares what it sent with what the hooks see. An observed event that is not injected, and that comes after `t_start` or `t_resume`, counts as the board's own if it matches one of these rows:

| observed | counts as the board's own if |
|---|---|
| move | within 2 px of a position sent in the last 500 ms, or within 10 px of the last position sent (before the first move: the cursor at start or resume). Small nudges are harmless because every board command carries x,y |
| key down | its usage is currently held by the replay (Windows auto-repeat) |
| key up | released by the replay in the last 500 ms, or physically held at `t_start`/`t_resume` (the Enter or Space that pressed Replay or Resume) |
| button down/up | the same button and direction was sent in the last 500 ms; a button up also counts if that button was physically held at `t_start`/`t_resume` |
| wheel | a wheel step was sent in the last 500 ms |
| (0x2A/0x36, extended) | always ignored (fake shifts) |

Anything else, unmapped keys included, means you touched the mouse or keyboard, and the replay auto-pauses.

**Echo check.** Every `KD`, `KU`, `BD`, `BU` and `W` sent must show up in the hooks within 300 ms. If one does not, the replay auto-pauses with "input hooks are not seeing the replay (admin window focused, secure desktop, or hook dropped)". Touch detection and the kill hotkey therefore never go silently dead while the replay keeps running.

**Auto-pause:**

1. If a mouse button is held and you are not physically holding Ctrl or Alt: release the held keys and tap Esc (`KD 41`, `KU 41`). This cancels a drag-and-drop or window move instead of dropping at a midpoint.
2. Send `R`.
3. Remember three things:
   - the held keys;
   - the resume index: the `bdown` step of a held button, so Resume redoes the drag from its start; otherwise the next step;
   - the target window.
4. Set the state to `paused`, with the reason.

**Resume** (GUI):

1. Set `t_resume = now` and recreate the listeners.
2. Return focus to the target app:
   - The board taps Alt; an Alt press lifts Windows' foreground lock. The tap is registered as expected input.
   - Call `SetForegroundWindow(target)`, then check `GetForegroundWindow() == target`.
   - If the check fails, stay paused with "could not return to <window title>". If M8b shows this approach fails, try `AttachThreadInput` instead.
3. Send `M` to the last replayed position (or to the drag's start), wait 30 ms, and re-press the remembered keys.
4. Add the pause length to `shift` and continue from the resume index.

**Stop** (GUI), the **kill hotkey Ctrl+Alt+Esc**, **`!HALT`**, **`!TIMEOUT`**, **`ERR`** and **board lost** all do the same thing:

1. Set the stop flag.
2. Apply the Esc rule from auto-pause step 1.
3. Send `R` if the board is online.
4. Go to `idle` and show the reason in the GUI.

If the board is lost while the replay held anything, the server also sends SendInput key-ups (scan codes from keymap) and button-ups for exactly those items, in case Windows keeps them down after the device disappears.

```
 idle --Ctrl+Alt+R (start_recording)--> recording --Ctrl+Alt+R (stop_recording: cut, save)--> idle
 idle --start_replay (HELLO)--> replaying --touch / echo miss / secure desktop / late--> paused
 paused --resume (refocus, move back, re-press)--> replaying
 replaying | paused --end / stop / kill / !HALT / !TIMEOUT / ERR / board lost--> idle
```

Recording and replay are mutually exclusive through this state machine.

## 8. SQLite schema (`data/picomp.db`)

- `PRAGMA journal_mode=WAL` runs once at startup.
- Every connection sets `PRAGMA foreign_keys=ON` and keeps the default 5 s busy timeout.

```sql
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS recordings (
  id          INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,          -- UTC, at t0 (the start hotkey)
  screen_w    INTEGER NOT NULL,          -- physical px
  screen_h    INTEGER NOT NULL,
  dpi         INTEGER NOT NULL,          -- primary monitor effective DPI (GetDpiForMonitor), 120 = 125%
  duration_ms INTEGER NOT NULL,
  shots_mode  TEXT    NOT NULL CHECK (shots_mode IN ('each_click','start_end','none'))
);

CREATE TABLE IF NOT EXISTS steps (
  recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,          -- replay order, 0..n-1
  t_ms         INTEGER NOT NULL,          -- ms since t0
  kind         TEXT    NOT NULL CHECK (kind IN ('move','bdown','bup','wheel','kdown','kup')),
  x            INTEGER,                   -- px: move, bdown, bup, wheel
  y            INTEGER,
  code         INTEGER,                   -- bdown/bup: button 1-5; kdown/kup: HID usage
  wheel_v      INTEGER,                   -- wheel: notches, + = up
  wheel_h      INTEGER,                   -- wheel: notches, + = right
  PRIMARY KEY (recording_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS screenshots (
  recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  seq          INTEGER NOT NULL,          -- each_click: its bdown step; start_end: 0 = start, n = stop
  png          BLOB    NOT NULL,
  PRIMARY KEY (recording_id, seq)
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('screenshots', 'each_click');
```

Steps store pixels and HID usages, which a v2 step editor can show and edit. HID units exist only on the wire. Screenshots have their own table, so step scans stay small.

## 9. GUI

`static/index.html` is one page with vanilla JS and `fetch`: no build step, no CDN, no templates. It polls `GET /api/status` every 500 ms. It shows:

- **Status line:** the board (connected, offline, halted with "next Replay re-arms", or an error) and the state (idle with "press Ctrl+Alt+R to record", "Recording... press Ctrl+Alt+R to stop", replaying step i of n, or paused with its reason).
- **Buttons:** Resume (paused) and Stop replay (replaying or paused). Recording has no buttons. After a recording stops, the status message shows the step count, the shot count and the number of injected inputs ignored.
- **Screenshots setting:** each click, start+end, or none.
- **Recordings table:** name, created, duration, steps, shots, plus Rename (inline), Delete (`confirm()`) and Replay.
- **Locking:** while the state is not `idle`, the table and the setting are disabled. The server returns 409 to every mutating request except replay resume/stop, so a replayed click on the GUI cannot change data.

| method and path | body / result |
|---|---|
| `GET /auth?t=` | sets the cookie, redirects to `/` |
| `GET /` | index.html |
| `GET /api/status` | `{board, state, replay:{id, step, total}, message}` |
| `GET /api/recordings` | list with counts |
| `PATCH /api/recordings/{id}` / `DELETE /api/recordings/{id}` | `{name}` / 204 (409 when not idle) |
| `POST /api/recordings/{id}/replay` | 202, or 409 with the reason |
| `POST /api/replay/resume` / `POST /api/replay/stop` | 204 |
| `GET /api/settings` / `PUT /api/settings` | `{screenshots}` (PUT returns 409 when not idle) |

## 10. Safety summary

| situation | what releases keys and buttons | worst case |
|---|---|---|
| normal end, Stop, kill hotkey, auto-pause | server sends `R` (Esc first if a button is held mid-drag) | none |
| Ctrl+C on the server | lifespan shutdown: stop the replay, `R`, save the recording, close the port | none |
| server crash | the port closes and DTR drops, so the firmware releases (M4); otherwise the 1 s heartbeat | about 1 s of auto-repeat |
| server hung | PING is sent only while the dispatcher and replay threads are alive, so the board releases after 1 s | about 1 s |
| mpremote Ctrl-C to the board | firmware `finally: release_all()` | none |
| host stops polling mid-send | dirty interface is resent until delivered | none |
| BOOT pressed | firmware releases everything and HALTs; the server sees `!HALT` and stops | none |
| unplug mid-replay | server stops the replay and sends SendInput ups for anything the replay held; reconnects later | none |
| UAC prompt or lock screen | the cursor check and echo check pause the replay; a recording's missed ups come from `GetAsyncKeyState` | at most one step reaches the secure desktop |
| elevated window focused | the echo check pauses the replay; recordings have gaps there (hooks are blind) | documented limitation |
| Windows silently drops a hook | the echo check pauses the replay; listeners are recreated at replay start and Resume | Ctrl+Alt+R does nothing; Ctrl+C the server (saving any recording) and restart it |
| monitor plugged in or scaling changed | recording and replay refuse to start; a replay pauses; a recording is discarded | none |

## 11. Testing

**pytest, no hardware.** Pure logic only. The player and recorder take their OS calls (send, clock, cursor, foreground window, key state) as injected callables, so tests use stubs.

- **`firmware/proto.py` under CPython:**
  - valid commands, bad arguments and ranges;
  - overlong lines, `\r\n`, partial chunks joined into lines;
  - the halted and no-session rules.
- **`board.py` helpers:**
  - command formatting and reply parsing;
  - lines split across reads;
  - the handshake skipping stale `PONG`, `!` and `ERR` lines.
- **Player:**
  - the command sequence, and timing with a fake clock;
  - the late-step shift and the more-than-1 s pause;
  - no Esc under Ctrl+Alt, and `R` at the end;
  - pause (the Esc rule, resume index at the drag's `bdown`);
  - resume (Alt tap, refocus check, move back, re-press);
  - stop and kill;
  - the echo-timeout pause.
- **Touch detector:**
  - auto-repeat and fake shifts ignored;
  - the 2 px and 10 px move rules;
  - a foreign click, key-down, wheel or unmapped key causes a pause;
  - events before `t_resume` ignored, and ups of keys held at start ignored.
- **Recorder:**
  - 60 Hz thinning keeps the last position; wheel accumulation;
  - repeat and orphan filtering (the start chord's ups dropped); injected count; fake shifts dropped; missed-up insertion;
  - the stop cut (the chord's Ctrl and Alt and trailing moves removed) and the shutdown cut;
  - `start_end` shots stored at seq 0 and n.
- **Hotkeys** (stubbed `GetAsyncKeyState`): Ctrl+Alt+R toggles only in idle and recording; its repeats and up are suppressed and never toggle again; R and Esc without Ctrl+Alt pass through.
- **`win.py` mapping:** 0 maps to 0 and W-1 maps to 32767, clamping, and a round trip within 1 px.
- **`keymap.py`:** one-to-one over the kept usages: 4-101 except 0x32 (Windows gives it the same scan code as 0x31) and any other collision found in M3b, plus 224-231.
- **`db.py`:** schema, cascade deletes, WAL on.
- **web:** one test file using `TestClient(app, base_url="http://127.0.0.1:8765")`. It checks:
  - 401 without the cookie, and `/auth` sets it;
  - a POST with `Origin: http://127.0.0.1:3000` gets 403;
  - a wrong `Host` header gets 400;
  - a mutation while not idle gets 409.

BoardLink connect, reconnect and halt handling are tested on the hardware (M6, M8a).

## 12. Milestones

Each milestone ends with a test you run. The starting point is today's state: `led_test.py` and `wiggle.py` work on the XIAO RP2350 build.

1. **M0: env and libraries.**
   - Work:
     - `conda env update`.
     - Vendor `keyboard.py` and `cdc.py`, fix `usb/README.md`, add `.gitignore`.
     - `cp -r firmware/usb :`.
   - Test: `python -m mpremote connect COM6 exec "import usb.device.keyboard, usb.device.cdc; print('ok')"` prints `ok`.
2. **M1: LED and BOOT button.**
   - Work: `led.py`.
   - Test: `python -m mpremote connect COM6 exec "import rp2,time; [print(rp2.bootsel_button()) or time.sleep_ms(250) for _ in range(40)]"`.
   - It passes only if it prints 0 while BOOT is released **and** 1 while BOOT is held. Otherwise drop the BOOT kill.
3. **M2: composite enumeration from RAM.**
   - Work: `picomp_fw.py` (init, plus a loop that answers HELLO and PING), `absmouse.py`, `proto.py`.
   - Run: copy them, then `exec "import picomp_fw; picomp_fw.run()"`. mpremote errors when the board re-enumerates; that is expected.
   - Test:
     - Device Manager shows 2 COM ports, a HID keyboard and a HID-compliant mouse.
     - `fwcheck ports` shows `:x.0` and `:x.2`, and `fwcheck hello` prints `OK PICOMP 1 …`.
     - `mpremote connect <REPL port> resume repl` still works, and RESET returns the board to a plain REPL.
     - If stale devices appear, uninstall the hidden devices in Device Manager.
4. **M3a: mouse.**
   - Work: `M`/`BD`/`BU`/`W` in the firmware; `board.py`; pixel-to-HID in `win.py`.
   - Test:
     - `fwcheck corners`: the cursor lands at (0,0), (1919,1079) and the centre, and `GetCursorPos` is printed.
     - `fwcheck click 960 540`.
     - `fwcheck scroll 0 -3`.
     - `fwcheck scroll 3 0` and `fwcheck scroll -3 0` in a wide document. If the horizontal direction is reversed, flip the sign in the firmware's `W` handler.
5. **M3b: keyboard.**
   - Work: `KD`/`KU`; `keymap.py`.
   - Test: `fwcheck keyscan` sends every usage while a suppressing hook logs (scan, extended, injected). It checks four things:
     - `keymap.py` matches what the hook sees;
     - board events arrive with injected=False;
     - which usages collide (expect 0x31 and 0x32);
     - whether Shift+arrow from the board produces fake shifts.
   - Also: `fwcheck hold 4` shows auto-repeat key-downs in the hook, and `fwcheck keys 11 8 15 15 18` types "hello" into Notepad.
6. **M4: firmware safety.**
   - Work: session, heartbeat, DTR, BOOT halt and HELLO re-arm, `R`, dirty resend, `finally` release, LED states.
   - Test:
     - `fwcheck hold 4 --no-ping` in Notepad: the repeat stops within 1 s.
     - `fwcheck hold 4`, then kill fwcheck from Task Manager: note whether the release is immediate (DTR) or takes 1 s.
     - `fwcheck hold 4`, then `python -m mpremote connect <REPL port> resume exec "pass"`: the repeat stops.
     - BOOT during a hold: the repeat stops and the LED goes red; `M` gets `ERR M halted` until `HELLO`.
     - `M` without `HELLO` gets `ERR M no-session`.
7. **M5: boot from flash.**
   - Work: `main.py`; copy it with `resume`, then `reset`.
   - Test:
     - Unplug and replug: the LED goes blue, and `fwcheck hello` works without mpremote.
     - `resume cp … :` followed by `reset` updates the firmware.
     - `resume rm :main.py` followed by `reset` gives a plain REPL, which proves the recovery path.
8. **M6: server skeleton.**
   - Work: `__main__` (DPI, token, startup), `web.py` (auth and Origin check), `db.py`, controller status, BoardLink with PING and reconnect, the GUI shell.
   - Test:
     - `python -m picomp` opens the GUI showing "board connected".
     - Unplugging shows offline within about 1.5 s, and replugging reconnects.
     - A private window gets 401.
     - Ctrl+C exits cleanly.
     - The web tests pass.
9. **M7: recording.**
   - Work: `hooks.py` with the Ctrl+Alt+R toggle, `recorder.py`, screenshots, the GUI recording state, list/rename/delete, the screenshots setting.
   - Test:
     - **First:** scroll with the touchpad and with the hub's mouse wheel, in a browser and in Explorer. Compare the result with the wheel steps and the injected count. If touchpad scrolling is missed, the README says "scroll with a mouse wheel while recording" and the overview is updated.
     - Start and stop with Ctrl+Alt+R, recording opening Notepad from Start, typing (including a Shift+Arrow selection on the laptop keyboard), a drag-select and a scroll. The saved steps contain no Ctrl, Alt or R from either chord.
     - In a page that logs `keydown`, Ctrl+Alt+R shows only Ctrl and Alt and toggles once, even held over 1 s with Ctrl released first. After Ctrl+Alt+Del and Cancel, a plain R still reaches the page.
     - Rename and delete work.
     - Ctrl+C mid-recording saves the recording without the Ctrl+C.
10. **M8a: blind replay.**
    - Work: `player.py` timing and sending, GUI Stop, kill hotkey, BOOT handling.
    - Test:
      - The M7 recordings replay the same, with the Shift+Arrow selection intact and nothing of either Ctrl+Alt+R chord replayed.
      - GUI Stop, Ctrl+Alt+Esc (the foreground app receives no Esc) and BOOT (red LED) each stop the replay.
      - The next replay re-arms.
11. **M8b: touch detection, Resume and edge cases.**
    - Work: touch detection, echo check, auto-pause, Resume with refocus, the SendInput fallback.
    - Test:
      - A mouse bump pauses the replay. Resume returns focus to Notepad, and the typing continues there.
      - A keypress pauses it, and Stop cancels it.
      - A pause mid-drag in Explorer cancels the drop, and Resume redoes the drag.
      - Unplugging mid-drag leaves nothing stuck, and the server recovers on replug.
      - Focusing Task Manager (elevated) during a replay pauses it through the echo check.
      - A recording that opens a UAC prompt (Win+R, `cmd`, Ctrl+Shift+Enter) has its Ctrl and Shift released, and its replay pauses with "secure desktop" or the echo reason.

## 13. Later

- **MCP:** a `picomp/mcp_tools.py` built on `mcp>=2.3,<3` (`MCPServer`), mounted in the same FastAPI app. Its tools call `Controller` and `win.py`, so nothing above changes.
- **v2:** a step view and editor in the GUI (steps and click screenshots are already stored); recording through the USB-A port as a USB host passthrough (C/C++ firmware, R13 removed).
- **Later still:** glides, humanized typing, a relative mouse (interface 6; the data CDC stays at 2), fast mode, recording LLM commands, and LLM-supervised replay.

## 14. Open items that can change the design

| uncertain | settled by | if it fails |
|---|---|---|
| Composite device with class 0xEF/02/01 gives both COM ports | M2 | check the descriptors before going further |
| Windows maps 0..32767 onto the full screen | M3a corners | adjust `win.py` |
| `rp2.bootsel_button()` works on this board | M1 (0 and 1) | no BOOT kill |
| DTR drops when the server process dies | M4 | the 1 s heartbeat is the backstop |
| Board events arrive as injected=False; auto-repeat and fake shifts are visible to hooks | M3b | touch detection rules depend on these; adjust the ignore list |
| Hooks capture touchpad two-finger scrolling | M7 first check | document "use a mouse wheel while recording" |
| An Alt tap plus `SetForegroundWindow` restores focus | M8b | `AttachThreadInput`; otherwise stay paused with the reason |
| Hooks are blind over elevated windows | known | the echo check pauses; documented limitation (server not elevated in v1) |

# PiCompMCP

PiComp records what you do with your own mouse and keyboard on this Windows laptop, then replays it once, blind, through a Waveshare RP2350-USB-A board that Windows sees as a real USB keyboard and absolute mouse.

- **Recording** happens in software: Windows low-level hooks (pynput) capture your real mouse and keyboard, and the touchpad's pointer and clicks.
- **Replay** goes out through the board, so to Windows it is ordinary hardware input.
- **The server** is one Python process, `python -m picomp`. It owns the board's data port, the hooks, SQLite (`data/picomp.db`, git-ignored) and a browser GUI on `http://127.0.0.1:8765`.

v1 has no MCP or LLM. Every action goes through `picomp/controller.py`, so MCP tools can call the same methods later. `PLAN.md` is the design. `TESTPLAN.md` walks through bring-up and testing.

## Hardware

| part | role |
|---|---|
| Waveshare RP2350-USB-A (RP2350A, 2 MB flash) | plugs into the laptop through its **USB-C** port (native USB). Its USB-A port stays empty in v1, because MicroPython has no USB host mode |
| RGB LED (WS2812, GPIO16) | status, see [LED](#led) |
| BOOT button | kill switch while the firmware runs: releases everything and halts the board |
| RESET button | restarts the board (runs `main.py` if it exists) |
| laptop | Windows 11. PiComp works on the main display; other screens can stay connected. Your keyboard and mouse (on a hub) plug into the laptop, not into the board |

While the firmware runs, the board is one composite USB device (VID 2E8A, PID 0005):

- interfaces 0-1: the MicroPython REPL serial port, used by mpremote;
- interfaces 2-3: the data serial port, which carries the server's commands (pyserial location ends in `:x.2`);
- interface 4: HID keyboard;
- interface 5: HID absolute mouse.

## One-time setup

### 1. Python environment

```
conda env update -n picomp -f environment.yml
conda activate picomp
```

Run everything below from the repo root, inside this env. Always run mpremote as `python -m mpremote`.

### 2. MicroPython

The board already runs MicroPython v1.29.0, the **SEEED_XIAO_RP2350** build (an RP2350 build for 2 MB flash, which fits this board). To flash it again, see [Recovery](#recovery).

### 3. Finding the ports

`python tools/fwcheck.py ports` lists the board's serial ports:

- **Before the firmware runs**, there is one: the REPL (COM6 on this laptop), listed as `other (REPL?)`.
- **Once it runs**, there are two:
  - `data` (location `...:x.2`) is the server's port;
  - `other (REPL?)` (location `...:x.0`) is the REPL port, which mpremote uses.

COM numbers can change, so check them instead of trusting them. Below, `<REPL port>` means the `other (REPL?)` row.

### 4. Copying the firmware

Do these steps in order:

1. **The USB library.** It is already on the board, so redo this only after a flash-nuke.

   ```
   python -m mpremote connect COM6 cp -r firmware/usb :
   ```

2. **The firmware modules.**

   ```
   python -m mpremote connect COM6 resume cp firmware/led.py firmware/proto.py firmware/absmouse.py firmware/picomp_fw.py :
   ```

3. **Prove it from RAM** before any `main.py` exists:

   ```
   python -m mpremote connect COM6 exec "import picomp_fw; picomp_fw.run()"
   ```

   mpremote then fails because the board re-enumerates as the composite device. That is expected. Check that:

   - the LED is blue;
   - `python tools/fwcheck.py ports` lists two ports;
   - `python tools/fwcheck.py hello` prints `OK PICOMP 1 0.1.0`.

   RESET (or a replug) brings back the plain REPL. If anything failed here, RESET is all the recovery you need.

4. **Install `main.py` last.** From then on the firmware starts at every power-up.

   ```
   python -m mpremote connect <REPL port> resume cp firmware/main.py :main.py
   python -m mpremote connect <REPL port> resume reset
   ```

**mpremote rules once `main.py` exists:**

- **Always pass `resume`**, `reset` included. Without it, mpremote soft-resets the board, and the USB device drops in the middle of the command; a plain `reset` then never gets to the hard reset and leaves a plain REPL.
- **Any mpremote command interrupts the firmware** (with Ctrl-C), even with `resume`. The firmware releases every key and button, the LED goes dark, and it stays stopped until `reset` or a replug. End every mpremote session with `python -m mpremote connect <REPL port> resume reset`.
- **To update the firmware**, stop the server, copy the changed file, then reset:

  ```
  python -m mpremote connect <REPL port> resume cp firmware/picomp_fw.py :
  python -m mpremote connect <REPL port> resume reset
  ```

## Running the server

```
python -m picomp
```

The console prints `PiComp GUI: http://127.0.0.1:8765/auth?t=...`, and the browser opens that URL once the server is up.

- **The token changes at every start.** A tab left over from an earlier run shows "Not authorized: open the URL printed in the server console" until the new URL has been opened in the same browser, which replaces the cookie every tab there shares. Open the new URL.
- **Stopping.** Press Ctrl+C once and wait. That stops a running replay (the board gets `R`), saves a recording in progress and closes the port.
- **The data port is exclusive.** The server and `tools/fwcheck.py` cannot use it at the same time, so stop the server before using fwcheck. The server retries the board every second.

## Using it

The GUI page has these parts:

- **Status:** the board line, the state line and the latest message.
- **Replay controls:** **Resume** and **Stop replay**.
- **Screenshots setting.**
- **Recordings table:** each row has **Rename**, **Delete** and **Replay**.

Everything except Resume and Stop is locked unless the state is idle.

**Recording.**

- Press **Ctrl+Alt+R** to start, and **Ctrl+Alt+R** again to stop. The GUI has no record button. Neither chord ends up in the recording.
- A recording covers the main display only (see Limitations). It does not need the board.
- Start each recording by clicking into the window you will work in. A replay starts with the GUI's browser in front.
- Release everything before the stop chord. Anything still held when it starts (a drag, or a Ctrl kept down) is cut back to its press.
- After a stop, the message shows the steps and screenshots saved, the software-injected inputs ignored (remapping and automation tools produce these) and any unmapped keys dropped.
- If the main display's resolution or scaling changes, or a screen is plugged in, removed or moved, during a recording, the GUI warns at once and the stop discards the recording.

**Screenshots.** The setting is **each click** (the default), **start + end** or **none**. The screenshots are stored for a later step view; v1's table shows only their count.

**Replaying.**

- **Starting.** Click **Replay**, then take your hand off the mouse. A replay needs:
  - the state to be idle;
  - the board connected, or halted (Replay re-arms it);
  - the main display at the same resolution and scaling as the recording (other screens may stay connected).
- **Auto-pause.** The replay pauses with the reason shown in the GUI when:
  - you touch the mouse or keyboard;
  - the hooks stop seeing the board's input (an elevated window, the secure desktop, or a dropped hook);
  - the display changes;
  - the system stalls.
- **Resume.** Click **Resume**, then let go of the mouse. Resume returns focus to the window the replay was working in, re-presses any held keys, and starts an interrupted drag again from its press.
- **Stopping.** Any of these stops a replay:
  - **Stop replay** in the GUI.
  - **Ctrl+Alt+Esc.** The focused app never sees the Esc. Your Ctrl press pauses the replay first, which is expected.
  - **BOOT** on the board. The LED turns red, and the next Replay re-arms the board.
  - **Unplugging the board.** The server then releases, through SendInput, anything the replay held.

## LED

| colour | meaning |
|---|---|
| blue | firmware running, no session (the server is not connected) |
| green | session open, nothing held |
| orange | a key or mouse button is held |
| red | halted by BOOT (the next Replay or HELLO re-arms it), or an internal error since the last HELLO |
| dark | the firmware is not running: mpremote stopped it, or there is no `main.py`. Run `python -m mpremote connect <REPL port> resume reset` |

The LED keeps its last colour until something sets it again. The reliable sign that the firmware is running is that `fwcheck ports` lists two ports.

## tools/fwcheck.py

fwcheck drives the board's data port directly, for bring-up and diagnosis. Stop the server first. `python tools/fwcheck.py --help` lists every subcommand, and every subcommand except `ports` accepts `--port COMx`.

| command | what it does |
|---|---|
| `ports` | lists the board's ports and their roles |
| `hello` | HELLO and PING round-trip times |
| `corners [--dwell 1.0]` | moves the cursor to the four corners and the centre, then checks each with GetCursorPos |
| `click X Y [--button 1]` | clicks at pixel X,Y |
| `scroll V H` | wheel notches at the cursor. **V comes first** (+ is up), then H (+ is right) |
| `keyscan [--gap 0.08]` | presses every key and compares what the hook sees with `picomp/keymap.py` |
| `hold USAGE [--seconds 10] [--no-ping] [--wait 3]` | holds one key and counts the key-downs the hook sees; `--no-ping` lets the board's 1 s heartbeat fire |
| `keys USAGE... [--gap 0.05] [--wait 3]` | taps keys in order: `keys 11 8 15 15 18` types "hello" |
| `send LINE... [--no-hello] [--listen 0.5]` | sends raw protocol lines and prints the replies |

- **`keyscan` swallows all keyboard input, system-wide, for about 20 s.** Ctrl+C does not work until it ends. Run it twice, once with NumLock on and once with it off.
- **Scroll order.** PLAN.md's M3a examples are written H first, but fwcheck takes V first:
  - `scroll -3 0` is three notches down;
  - `scroll 0 3` is three notches right. If it scrolls left instead, set `_PAN_SIGN = -1` in `firmware/picomp_fw.py`, copy that file to the board and reset.

## Tests

```
python -m pytest
```

The tests cover pure logic only. They need no board, start no hooks and open no ports. `pytest.ini` limits collection to `tests/`.

## Recovery

Try these in order:

1. **Remove `main.py`.** This gives back a plain REPL. Then fix the problem and redo "Copying the firmware" from step 2.

   ```
   python -m mpremote connect <REPL port> resume rm :main.py
   python -m mpremote connect <REPL port> resume reset
   ```

2. **Re-flash**, if the REPL is unreachable:
   1. Hold BOOT while plugging the board in. It shows up as a USB drive.
   2. Copy the flash-nuke UF2 onto that drive: [`nuke_universal.uf2`](https://github.com/raspberrypi/pico-sdk-prebuilts/releases/latest/download/nuke_universal.uf2). It erases the whole flash, `main.py` included. If the drive does not come back by itself, hold BOOT and replug again.
   3. Copy the SEEED_XIAO_RP2350 v1.29.0 UF2 from <https://micropython.org/download/SEEED_XIAO_RP2350/>.
   4. Redo "Copying the firmware" from step 1.

   Re-flashing MicroPython without the nuke keeps the filesystem, and with it `main.py`.

**Stuck keys or buttons** release on their own: the board releases everything when the port closes, and after 1 s without traffic. BOOT releases everything at once.

## Limitations

- **Elevated windows and the secure desktop are blind spots.** The hooks see nothing over admin windows (Task Manager, for example), UAC prompts, the lock screen or Ctrl+Alt+Del:
  - a recording has gaps there;
  - a replay pauses, through the echo check or the cursor check.

  The server is not elevated in v1.
- **Key limits.** At most 6 keys plus the 8 modifiers can be held at once. A 7th key gets `ERR KD bad-args`, which stops the replay.
- **Unsupported keys.** F13-F24 and media or volume keys cannot be sent. While recording they are dropped and counted as unmapped.
- **US keyboard layout** is assumed by `picomp/keymap.py`.
- **The main display only.** Windows maps the board's absolute mouse onto the main display, so PiComp records and replays there. Pick it in Settings > System > Display > "Make this my main display". Other screens can stay connected:
  - mouse moves over them are left out of a recording, and a click or scroll on one discards it (the GUI warns at once);
  - the main display needs the same resolution and scaling at replay as at recording;
  - plugging in, removing or rearranging a screen discards a recording in progress and pauses a replay.
- **Touchpad scrolling is not recorded.** The hooks never see a two-finger swipe (checked in Chrome and Explorer), so the replay would not scroll and PiComp cannot warn you. Scroll with a mouse wheel while recording. The touchpad's pointer moves, taps and presses are recorded.
- **Mouse buttons are recorded as physical buttons.** If you swap Windows' primary mouse button between recording and replay, left and right clicks trade places. The server reads that setting at start and at every Replay and Resume.
- **Replay is blind.** It does not look at the screen. If a window opens more slowly than during the recording, later clicks land on whatever is there.
- **A server crash loses the recording in progress.** Ctrl+C saves it.
- **The data port is exclusive**, so the server and fwcheck cannot run at the same time.

## Project layout

```
PiCompMCP/
  PLAN.md  TESTPLAN.md  README.md  environment.yml  pytest.ini  .gitignore
  firmware/                 copied to the board (except led_test.py and wiggle.py)
    main.py                 import picomp_fw; picomp_fw.run()
    picomp_fw.py            composite USB init, main loop, safety (session, heartbeat, BOOT), LED
    proto.py                line framing and command parser (also runs under CPython for tests)
    absmouse.py             absolute HID mouse (7-byte report)
    led.py                  PIO WS2812 driver
    led_test.py wiggle.py   bring-up scripts (dev only)
    usb/device/             vendored micropython-lib USB device library, unmodified
  picomp/                   the server: python -m picomp
    __main__.py             DPI awareness, token, uvicorn on 127.0.0.1:8765
    controller.py           the state machine; every action is a method here
    board.py                serial link to the board (discovery, handshake, PING, reconnect)
    hooks.py                pynput low-level hooks, the Ctrl+Alt+R / Ctrl+Alt+Esc hotkeys
    recorder.py             hook events -> steps; screenshot worker
    player.py               replay timing, touch detection, echo check, pause/resume/stop
    keymap.py               HID usage <-> (scan code, extended), US layout
    win.py                  Win32 via ctypes; mss screenshots
    db.py                   SQLite schema and queries
    web.py                  FastAPI app: auth cookie, Origin/Host checks, REST API
    static/index.html       the GUI
  tools/fwcheck.py          drives the board directly (bring-up, diagnosis)
  tests/                    pytest, pure logic, no hardware
  data/picomp.db            SQLite database (created at first start, git-ignored)
```

## Where the code departs from PLAN.md

PLAN.md is kept as written. These points supersede it:

- **Mouse button swap (PLAN section 6).** PLAN checks buttons with VK_LBUTTON and VK_RBUTTON, swapped when SM_SWAPBUTTON is set. Instead, `hooks.py` undoes the swap once, as each button event arrives, so recorded codes are physical buttons and `win.button_vk` does not swap.
- **Scroll checks (PLAN milestone M3a)** are written in H, V order there. fwcheck takes V then H.
- **`pytest.ini`** at the repo root is not in PLAN section 2's layout. It keeps pytest from collecting the board script `firmware/led_test.py`.
- **`fwcheck send`** is not in PLAN. It is needed for the M4 no-session and halted checks.
- **`mpremote ... reset` (PLAN section 2)** is `resume reset` here, for the reason under "mpremote rules" above.
- **(0, 0) (PLAN section 3).** Windows ignores an absolute mouse report at exactly (0, 0), so `absmouse.py` sends x=1 there, which is still pixel 0.
- **Right Shift and fake shifts (PLAN sections 6 and 7).** PLAN maps Right Shift to (0x36, plain) and drops every (0x36, extended) as a fake shift. The US layout marks Right Shift extended (`T36 | KBDEXT` in kbdus.c), so every keyboard's Right Shift reports as (0x36, extended) with vk VK_RSHIFT, and `keymap.py` maps usage 229 to that. A PS/2 keyboard's fake E0 36 has no key in the layout, so its vk is not VK_RSHIFT, and `is_fake_shift` uses the vk to tell them apart. E0 2A is always fake. So are the Shift presses Windows makes up itself around a keypad key while NumLock is on (scan code with SCANCODE_SIMULATED, 0x200: 0x236 and 0x22A). SendInput gets Right Shift and NumLock without the extended flag (`send_scan_for`), because the layout adds it again.
- **Several screens (PLAN sections 6 and 7).** PLAN refuses to record or replay unless exactly one monitor is connected. Here any number may be connected and PiComp works on the main display, the only one the board's absolute mouse reaches: `win.display()` carries every screen's rectangle, the recorder leaves out moves over other screens and discards the recording on a click or scroll there, and screenshots grab the main display (mss lists the screens in Windows' order, which can start with another one).
- **SysRq and Break (PLAN section 6).** While Alt is down, Windows reports PrintScreen as scan 0x54, and while Ctrl is down it reports Pause as E0 46. `keymap.usage_for` maps those back to PrintScreen and Pause.

# PiComp test plan

You run every step yourself, in order. The sections follow the milestones in PLAN.md section 12, and each one builds on the one before, so stop at the first failure. Steps marked *(optional)* add confidence but are not needed to move on.

## Before you start

**Terminals.** Open two terminals (Anaconda Prompt, or Anaconda PowerShell Prompt) and run this in each:

```
cd D:\Projects\PiCompMCP
conda activate picomp
```

**T1** is the main terminal. **T2** is for the few steps that need a second one.

**Ports.** `python tools/fwcheck.py ports` lists the board's serial ports:

- `<REPL>` is the `other (REPL?)` row, which mpremote uses. Before the firmware runs, it is the only row (COM6 on this laptop).
- `<DATA>` is the `data` row (location `...:x.2`), the firmware's command port. fwcheck and the server find it by themselves.

When you type a command, replace `<REPL>` with the actual port name, such as `COM6`. PowerShell rejects a literal `<`.

**When a step fails, send me:**

- the step number and the exact command;
- its complete output (select the text in the console and copy it);
- what the LED and the GUI showed;
- for server steps, the last 40 or so lines of the server console.

A screenshot helps when the result is on the screen.

### Helpers

**H1: start the firmware from RAM.** You need this in M2 to M4, before `main.py` is installed. The firmware stops whenever you press RESET, replug the board or run any mpremote command. You can tell it has stopped when the LED is dark or `fwcheck ports` shows a single port. To start it again:

1. Press RESET (skip this if you just replugged), then wait 2 s.
2. Run this, where COM6 is the single port that `fwcheck ports` shows now:

   ```
   python -m mpremote connect COM6 exec "import picomp_fw; picomp_fw.run()"
   ```

3. mpremote ends with an error because the board re-enumerates. That is expected. The LED turns blue, and `python tools/fwcheck.py ports` lists two ports.

**H2: key log page.** This page shows every key-down and key-up it receives. Paste the line below into the address bar of a **new browser window** and press Enter:

```
data:text/html,<title>keys</title><pre id=o></pre><script>onkeydown=e=>{e.preventDefault();o.textContent+="down "+e.code+(e.repeat?" (repeat)":"")+"\n"};onkeyup=e=>{e.preventDefault();o.textContent+="up   "+e.code+"\n"}</script>
```

Click inside the page before you type. To clear the log, use the browser's reload button; the page captures F5.

**H3: steps of the newest recording.** This works with or without the server running.

Kind counts:

```
python -c "import sqlite3; c = sqlite3.connect('data/picomp.db'); print(c.execute('select kind, count(*) from steps where recording_id = (select max(id) from recordings) group by kind').fetchall())"
```

Every step:

```
python -c "import sqlite3; c = sqlite3.connect('data/picomp.db'); r = c.execute('select id, name from recordings order by id desc').fetchone(); print(r); [print(*s) for s in c.execute('select seq, t_ms, kind, x, y, code, wheel_v, wheel_h from steps where recording_id = ? order by seq', (r[0],))]"
```

- **Columns:** seq, t_ms, kind, x, y, code, wheel_v, wheel_h.
- **Button codes:** 1 left, 2 right, 3 middle.
- **Key codes** are HID usages:

  | keys | codes |
  |---|---|
  | letters | 4-29: a=4, c=6, d=7, e=8, h=11, l=15, m=16, o=18, r=21 |
  | Enter, Esc, Backspace, Space | 40, 41, 42, 44 |
  | Delete | 76 |
  | Right, Left, Down, Up | 79, 80, 81, 82 |
  | LCtrl, LShift, LAlt, LWin | 224, 225, 226, 227 |
  | RCtrl, RShift, RAlt | 228, 229, 230 |

For an older recording, change `order by id desc` to `where id = 3` (or whichever id).

---

## 0. Environment and automated tests

**0.1** Update the env:

```
conda env update -n picomp -f environment.yml
```

- **Expect:** it finishes without errors.

**0.2** Check the dependencies:

```
python -c "import serial, fastapi, uvicorn, pynput, mss, httpx, pytest; print('deps ok')"
python -m pip show fastapi mss pynput pyserial
```

- **Expect:** `deps ok`, and versions 0.143.0, 10.2.0, 1.8.2 and 3.5.

**0.3** Run the tests:

```
python -m pytest
```

- **Expect:** the summary line shows only `passed`, with no `failed` and no `error`. That is a few hundred tests (most are parametrized), taking a few seconds. They open no ports and install no hooks, so the board may be plugged in or not.
- **If not:** send the full output of `python -m pytest -rfE`. Stop here: every later step assumes the code imports and its logic passes.

## M0-M1 (already done)

M0 (the env, and the vendored USB library on the board) and M1 (the LED, and BOOT readable as 0/1) passed earlier.

*(optional)* Re-check M0:

```
python -m mpremote connect COM6 exec "import usb.device.keyboard, usb.device.cdc; print('ok')"
```

- **Expect:** `ok`.

## M2. Composite enumeration from RAM

**M2.1** Run `python tools/fwcheck.py ports`.

- **Expect:** exactly one row: `COM6  other (REPL?)  location=...:x.0 ...`.
- **If none:** check the USB-C cable and press RESET.

**M2.2** Copy the firmware modules:

```
python -m mpremote connect COM6 resume cp firmware/led.py firmware/proto.py firmware/absmouse.py firmware/picomp_fw.py :
```

- **Expect:** one `cp` line per file, and no error.
- *(optional)* `python -m mpremote connect COM6 ls` lists them, plus `usb/`, and no `main.py`. If a `main.py` is there, run `python -m mpremote connect COM6 rm :main.py` before going on.

**M2.3** Start the firmware from RAM (H1 step 2).

- **Expect:** mpremote ends with an error, and the LED turns blue within a few seconds.
- **If not:** if mpremote printed a `Traceback`, send it. Press RESET to recover.

**M2.4** Open Device Manager (Win+X, then Device Manager).

- **Expect:**
  - Ports (COM & LPT) shows two `USB Serial Device (COMx)` entries for the board.
  - Keyboards gains a `HID Keyboard Device`, and Mice and other pointing devices gains a `HID-compliant mouse`. Compare with the lists after a RESET.
  - There are no yellow warning icons.
- **If not:** send a screenshot of Device Manager, plus the text of the failing device's Properties > Events tab.
- This step settles open item 1 (the composite device gives both COM ports).

**M2.5** Run `python tools/fwcheck.py ports`.

- **Expect:** two rows, `data` with `location=...:x.2` and `other (REPL?)` with `location=...:x.0`. Note both COM numbers. The `other (REPL?)` one is `<REPL>` from now on.

**M2.6** Run `python tools/fwcheck.py hello`.

- **Expect:**

  ```
  COM7: OK PICOMP 1 0.1.0  (handshake 3.1 ms)
  COM7: PONG  (0.8 ms)
  ```

  Both times are well under 50 ms. The LED flashes green, then returns to blue when fwcheck closes the port.
- **If not:** send the output of `ports` and `hello`.

**M2.7** Check that the REPL still works:

```
python -m mpremote connect <REPL> resume repl
```

- **Expect:** mpremote connects but shows no prompt, because the firmware is running.
  1. Press Ctrl-C. A `KeyboardInterrupt` traceback and a lone `>` appear, and the LED goes dark (the firmware stopped and released everything). The `>` is the raw REPL that H1's `exec` started the firmware from; nothing you type there is echoed.
  2. Press Ctrl-B. The MicroPython banner and the `>>>` prompt appear.
  3. Type `1+1` and press Enter. It prints `2`.
  4. Leave with Ctrl-X (or Ctrl-]).

**M2.8** Press RESET.

- **Expect:** `python tools/fwcheck.py ports` shows one port again.
- If stale or duplicate devices pile up over the next steps, use Device Manager > View > Show hidden devices, and uninstall the greyed-out ones.

## M3a. Mouse

Start the firmware first (H1).

**M3a.1** Run `python tools/fwcheck.py corners`.

- **Expect:**
  - the handshake line, then `screen WxH physical px` (your physical resolution, such as `1920x1080`);
  - the cursor jumps to top-left, top-right, bottom-right, bottom-left and the centre, for 1 s each;
  - five lines like `target (x, y)  HID (hx, hy)  cursor (x, y)  ok`;
  - last line: `all corners ok`.
- **If any point is `OFF`:** send the whole output, plus the resolution and scale from Settings > System > Display.
- This step settles open item 2 (Windows maps 0..32767 onto the full screen).

**M3a.2** Set up and click:

1. Open Notepad and place it so it covers the middle of the screen, with T1 still visible beside it.
2. Click in T1, then run `python tools/fwcheck.py click 960 540`. Use the centre `target` from M3a.1 if your screen is not 1920x1080.

- **Expect:** the cursor jumps to the centre and clicks there. Notepad comes to the front with its caret at that spot. T1 prints `clicked button 1 at (960, 540), HID (...); cursor now (960, 540)`.
- *(optional)* `python tools/fwcheck.py click 960 540 --button 2` opens Notepad's context menu. Press Esc to close it.

**M3a.3** Vertical scroll. Scrolling uses Windows' "Scroll inactive windows when I hover over them" setting (Settings > Bluetooth & devices > Mouse), which is on by default.

1. Open a long page, for example `https://en.wikipedia.org/wiki/Microsoft_Windows`, in a browser window that does not cover T1.
2. Click in T1, then move the pointer over the page **without clicking**.
3. Run `python tools/fwcheck.py scroll -3 0`.

- **Expect:** the page scrolls down three notches, about the same as three clicks of a mouse wheel.

Then run `python tools/fwcheck.py scroll 3 0`. The page scrolls back up.

**M3a.4** Horizontal scroll.

1. Zoom the same page to about 300% (Ctrl and + several times) so it gets a horizontal scrollbar.
2. Put the pointer over the page, without clicking.
3. Run `python tools/fwcheck.py scroll 0 3`.

- **Expect:** the page scrolls **right**. Then `python tools/fwcheck.py scroll 0 -3` scrolls it left.
- **If `scroll 0 3` scrolls left:**
  1. Set `_PAN_SIGN = -1` in `firmware/picomp_fw.py`.
  2. Run `python -m mpremote connect <REPL> resume cp firmware/picomp_fw.py :`.
  3. Start the firmware again (H1) and repeat M3a.4.
  4. Tell me you flipped it.

## M3b. Keyboard

The firmware must be running (H1).

**NumLock state check** (used below):

```
python -c "import ctypes; print('NumLock', 'on' if ctypes.WinDLL('user32').GetKeyState(0x90) & 1 else 'off')"
```

To toggle NumLock through the board, run `python tools/fwcheck.py keys 0x53 --wait 0`.

**M3b.1** keyscan with NumLock **on**.

1. Use the state check, and toggle NumLock if needed.
2. Run `python tools/fwcheck.py keyscan`.
3. **Do not touch the keyboard for about 25 s.**

- **Expect:**
  - `keyboard input is now swallowed ... do not type`, then the hex codes `04` ... `E7` appear, then `keyboard released`.
  - Nothing reacts on screen while it runs: no Start menu, no Snipping Tool. Every key is swallowed.
  - A table with one row per usage. Every row reads `ok`, except row `0x32`, which reads `not in keymap`, down `2B ...`, records as `0x31`, result `-`.
  - No row says `MISMATCH`, `none seen` or `INJECTED`.
  - Under `collisions (one scan code from several usages):`, only `2B: 0x31, 0x32`.
  - The line `0 mismatches, 0 keymap usages not seen, 0 usages flagged injected`.
  - A `Shift+Right from the board` section that lists the events. Some lines may be marked `FAKE SHIFT`.
- **Send the full output in any case.** It decides corrections to `picomp/keymap.py` and how fake shifts are handled.
- Afterwards, check that CapsLock and ScrollLock are off.

**M3b.2** keyscan with NumLock **off**.

1. Run `python tools/fwcheck.py keys 0x53 --wait 0`.
2. Confirm with the state check.
3. Run `python tools/fwcheck.py keyscan` again.

- **Expect:** the same table. The Shift+Right section may differ.
- **Send the full output.** Then put NumLock back the way you like it.
- M3b.1 and M3b.2 settle open item 5 (board keys arrive with injected=False, and fake shifts are visible).

**M3b.3** Open an empty Notepad, run `python tools/fwcheck.py hold 4`, and click into Notepad's text area during the 3 s countdown.

- **Expect:**
  - Notepad shows one `a`, a short pause, then a fast `aaaa...` for 10 s, and the repeat stops at the end.
  - T1 prints `> KD 4  (holding 10 s)`, then every second `hook: N key-downs (0 injected)`, with N rising by about 30 per second.
  - It ends with `total: ~300 key-downs seen by the hook (0 injected), last one +9.9.. s after KD`.
  - The LED is orange while the key is held.
- **If `injected` is above 0, or N stays at 1:** send the output.
- This step also settles open item 5 (Windows auto-repeat is visible to the hooks).

**M3b.4** Run `python tools/fwcheck.py keys 11 8 15 15 18`, and click into Notepad during the countdown.

- **Expect:** `hello` is typed.

## M4. Firmware safety

The firmware must be running (H1). Keep an empty Notepad open, and clear it between tests (Ctrl+A, then Delete).

**M4.1** Heartbeat. Run `python tools/fwcheck.py hold 4 --no-ping`, and click into Notepad during the countdown.

- **Expect:**
  - the `a` repeat stops within about 1 s of the press;
  - T1 shows `< !TIMEOUT` about 1.0 s after the `> KD 4` stamp, and the hook count stops rising after that;
  - the LED goes orange, then green.

**M4.2** Does DTR drop when a process dies?

1. In T2, which must be **PowerShell**, paste this. It starts a 15 s timer, then kills fwcheck only:

   ```
   Start-Sleep 15; Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*fwcheck*' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```

2. Within those 15 s, run `python tools/fwcheck.py hold 4 --seconds 30` in T1, and click into Notepad during the countdown.
3. Watch Notepad and the LED when the timer fires.

- **Expect one of two results:**
  - **The repeat stops at once and the LED turns blue.** DTR dropped when the process died, so a crashed server releases everything immediately. This is the best case.
  - **The repeat runs about 1 s longer, then stops, and the LED turns green.** DTR did not drop, and the 1 s heartbeat released everything. This also passes.
- **Tell me which one you saw.** This settles open item 4 (DTR drops when the server process dies).
- **Failure:** the repeat goes on for more than 2 s. Unplug the board to stop it, and send what you saw.

**M4.3** Does an mpremote interrupt release everything?

1. In T2, with the env active, start a delayed interrupt. In PowerShell:

   ```
   Start-Sleep 15; python -m mpremote connect <REPL> resume exec "pass"
   ```

   In cmd (Anaconda Prompt):

   ```
   timeout /t 15 && python -m mpremote connect <REPL> resume exec "pass"
   ```

2. In T1, run `python tools/fwcheck.py hold 4 --seconds 30`, and click into Notepad during the countdown.

- **Expect:**
  - when mpremote runs, the repeat stops at once and the LED goes dark (the firmware's `finally` released everything);
  - fwcheck keeps running with its counts no longer rising. It may end early with `serial error: Write timeout`, which is expected because nothing reads the port any more.
- Then start the firmware again (H1).

**M4.4** BOOT halt.

1. Run `python tools/fwcheck.py hold 4 --seconds 20`, and click into Notepad during the countdown.
2. While it repeats, press BOOT once.

- **Expect:** the repeat stops at once, T1 shows `< !HALT`, and the LED turns red and stays red after fwcheck exits.

Then run these, in order:

| step | command | expect |
|---|---|---|
| a | `python tools/fwcheck.py send --no-hello "M 0 0"` | `< ERR M halted`; the cursor does not move; the LED stays red |
| b | `python tools/fwcheck.py hello` | OK and PONG (HELLO clears HALTED); the LED is blue after fwcheck exits |
| c | `python tools/fwcheck.py send --no-hello "M 0 0"` | `< ERR M no-session`; the cursor does not move |
| d | `python tools/fwcheck.py send "M 0 0"` | the handshake line, no reply lines, and the cursor jumps to the top-left corner |

M4.4 also confirms open item 3 (BOOT works while USB is busy).

**M4.5** *(optional)* Protocol errors:

```
python tools/fwcheck.py send "KD 300" "W 200 0 0 0" "FOO" "M 1"
python tools/fwcheck.py send "M 00000000000000000000000000000000000000000000000000 0"
```

- **Expect:**
  - from the first command: `ERR KD bad-code`, `ERR W bad-args`, `ERR FOO unknown` and `ERR M bad-args`;
  - from the second: `ERR - too-long`.

By now you have seen every LED state:

- blue: no session;
- green: session open, nothing held;
- orange: something held;
- red: halted;
- dark: the firmware is stopped.

The dirty-resend path (the host not polling the board) cannot be triggered by hand. Only code review covers it.

## M5. Boot from flash

**M5.1** Install the firmware.

1. If `fwcheck ports` shows two ports, use `<REPL>` below. Otherwise use COM6.
2. Run:

   ```
   python -m mpremote connect <REPL> resume cp firmware/led.py firmware/proto.py firmware/absmouse.py firmware/picomp_fw.py :
   python -m mpremote connect <REPL> resume cp firmware/main.py :main.py
   python -m mpremote connect <REPL> resume reset
   ```

- **Expect:** the board re-enumerates by itself. The LED turns blue, and `fwcheck ports` shows two ports, with no `exec` needed.
- Every `reset` needs `resume`. Without it, mpremote soft-resets first, which drops the firmware's USB device before the hard reset runs, and the board is left as a plain REPL.

**M5.2** Unplug the board, wait 3 s, then plug it back in.

- **Expect:** the LED turns blue within a few seconds, and `python tools/fwcheck.py hello` prints OK and PONG.

**M5.3** Update path:

```
python -m mpremote connect <REPL> resume cp firmware/picomp_fw.py :
python -m mpremote connect <REPL> resume reset
```

- **Expect:** the LED goes dark after the first command and blue after the reset. `fwcheck hello` still works.

**M5.4** Recovery path:

```
python -m mpremote connect <REPL> resume rm :main.py
python -m mpremote connect <REPL> resume reset
```

- **Expect:** `fwcheck ports` shows a single port, which means a plain REPL. The recovery path works.

Then reinstall. COM6 here is the single port shown now:

```
python -m mpremote connect COM6 cp firmware/main.py :main.py
python -m mpremote connect COM6 resume reset
```

- **Expect:** two ports again, and the LED is blue.

## M6. Server skeleton

Stop every fwcheck first. The board stays plugged in, with its LED blue.

**M6.1** Start the server in T1:

```
python -m picomp
```

- **Expect in the console:**
  - `PiComp GUI: http://127.0.0.1:8765/auth?t=...`;
  - INFO lines including `input hooks started`, `controller started (database D:\Projects\PiCompMCP\data\picomp.db, screenshots each_click)`, `board connected: COM7, firmware 0.1.0` and `Uvicorn running on http://127.0.0.1:8765`.
- **Expect in the GUI**, which the browser opens by itself (paste the URL if it does not):
  - `Board connected (COM7, firmware 0.1.0)` with a green dot;
  - `Idle: press Ctrl+Alt+R to record`;
  - Settings: `Screenshots while recording` set to `each click`;
  - Recordings: `No recordings yet: press Ctrl+Alt+R to start one.`
- **Also:** the LED turns green, because a session is open.
- **If not:** send the console if it prints `could not enable per-monitor DPI awareness (got N)` and exits, or if the GUI shows `Board error: ...`.

**M6.2** Unplug the board.

- **Expect:** within about 1.5 s the GUI shows `Board offline` and the message `board disconnected`. The console shows a warning `board on COM7 lost: ...`.

Plug it back in.

- **Expect:** within a few seconds the GUI shows `Board connected (...)` again, the message clears, and the LED is green.

**M6.3** Authentication.

1. Open an InPrivate window (Ctrl+Shift+N) and go to `http://127.0.0.1:8765/`.
   - **Expect:** `{"detail":"not authorized: open the URL printed in the server console"}`.
2. Try `http://127.0.0.1:8765/auth?t=wrong`.
   - **Expect:** the same reply.
3. Paste the full URL from the console into the InPrivate window.
   - **Expect:** the GUI loads.
4. Close the InPrivate window.

**M6.4** *(optional)* Origin and Host checks, in PowerShell. Type `curl.exe`, not `curl`:

```
curl.exe -i -X POST http://127.0.0.1:8765/api/replay/stop -H "Origin: http://127.0.0.1:3000"
curl.exe -i http://127.0.0.1:8765/api/status -H "Host: evil.example"
```

- **Expect:**
  - the first command gets `403` with `{"detail":"bad origin"}`;
  - the second gets `400` with `Invalid host header`.

**M6.5** Press Ctrl+C once in T1.

- **Expect:**
  - within a couple of seconds, the console shows `input hooks stopped`, `controller stopped`, uvicorn's shutdown lines and `PiComp stopped`, then the prompt returns;
  - the LED turns blue;
  - the GUI tab shows `server not reachable` in red.
- **If it hangs for more than 5 s:** send the console.

**M6.6** Start `python -m picomp` again.

- **Expect:** the new tab works. The old tab may show `Not authorized: open the URL printed in the server console` for a moment, then shows the live status again: the new tab's sign-in replaced the cookie that every tab of the browser shares. A tab in another browser (or an InPrivate window) stays `Not authorized`.

Close the old tab, and keep the server running from here on.

**M6.7** In T2, run `python -m pip freeze` and send me the output. PLAN asks for the `>=` entries in `environment.yml` to be replaced with exact versions after M6.

## M7. Recording

**Set up once:**

- **Notepad.** Windows 11 Notepad reopens unsaved tabs by default. In Notepad's Settings, turn off continuing the previous session so it starts empty. Otherwise, always close it with "Don't save".
- **GUI window.** Give the PiComp GUI its own browser window where it stays visible: at one side of the main screen, or on another screen. Do not click it during recordings: on the main screen the click is recorded, and on another screen it discards the recording.
- **Several screens.** PiComp records and replays on Windows' main display only. Keep the windows you record in on the main screen.

**M7.1** Touchpad scroll. PLAN's first check for M7 compares four short recordings: the touchpad and the mouse wheel, each in a browser and in Explorer.

Repeat these steps for each of the four variants:

1. Open the target, scrolled to the top:
   - **browser:** `https://en.wikipedia.org/wiki/Microsoft_Windows`;
   - **Explorer:** `C:\Windows\System32`.
2. Put the pointer over the content and hold still.
3. Press Ctrl+Alt+R.
   - **Expect:** the GUI shows `Recording... press Ctrl+Alt+R to stop (0 steps)`.
4. Scroll down:
   - **mouse wheel:** exactly 5 notches;
   - **touchpad:** one two-finger swipe down.
5. Click once: on plain text in the browser, or on a file name in Explorer (that only selects it). The click keeps the recording from being empty, so the message shows the injected count.
6. Press Ctrl+Alt+R.
7. Note the message `Saved "...": N steps, 1 shots, I software-injected inputs ignored`, and run the H3 kind counts.

- **Expect, mouse wheel:** the counts include `('wheel', 5)` (more for a high-resolution wheel), and I is 0.
- **Expect, touchpad:** one of two results:
  - `wheel` steps appear: touchpad scrolling is captured;
  - no `wheel` entry: it is not captured, and the README keeps "scroll with a mouse wheel while recording".
- **Send the four messages and the four count lines.** This settles open item 6 (the hooks capture touchpad scrolling).
- Keep one mouse-wheel browser recording for M8a.2, and delete the rest (M7.5 shows how).

**M7.2** Hotkey behaviour. Open H2 in its own window next to the GUI, and click inside it.

| step | do | expect |
|---|---|---|
| a | Press Ctrl+Alt+R and release | the page shows `down ControlLeft`, `down AltLeft` and their ups, but **no `KeyR`**; the GUI shows `Recording...` |
| b | Press Ctrl+Alt+R again | again only Control and Alt on the page; the GUI shows `Saving recording...`, then idle with `Recording discarded: no steps`, which is correct because nothing happened in between |
| c | Hold Ctrl+Alt+R for 2 s, release Ctrl first, then R, then Alt | the page shows no `KeyR`, not even repeats; the GUI toggled **once**, to Recording. Press Ctrl+Alt+R once more to stop it (discarded) |
| d | Press Ctrl+Alt+Del and click Cancel. Click inside the page and press a plain R | the page shows `down KeyR` / `up KeyR`; the GUI stays idle, and no recording starts |

- **If not:** if an R reaches the page in steps a-c, the GUI toggles twice, or step d goes wrong, send the page log and the console.

**M7.3** Main recording, **R1** (Notepad). Notepad is closed when you start.

1. Press Ctrl+Alt+R.
2. Click the Start button, type `notepad` and press Enter. Wait until Notepad is open and empty.
3. Type `hello world`, press Enter, and type `second line`.
4. On the laptop keyboard, hold Shift, press Left four times and release Shift. That selects `line`. Type `row`. Line 2 now reads `second row`.
5. With the mouse, drag-select `hello` on line 1: press just left of the h, drag to just right of the o, and release. Then press Delete. Line 1 now reads ` world`.
6. Take your hands off for a second, then press Ctrl+Alt+R.

- **Expect in the GUI:**
  - `Saving recording...` briefly, then idle with the message `Saved "Recording YYYY-MM-DD HH:MM:SS": N steps, 2 shots, 0 software-injected inputs ignored`. Shots counts the mouse presses: 2 if you followed the script exactly;
  - a new top row in the table with Created, Duration, Steps and Shots;
  - the console shows no `unmapped key dropped` lines.
- **Check with H3 (every step):**
  - the first and last few steps contain no key code 224, 226 or 21, so both chords were cut out;
  - the last step is `kup` with code 76 (Delete);
  - the selection is there: `kdown 225`, then four `kdown 80` / `kup 80` pairs, then `kup 225`;
  - in the kind counts, `kdown` equals `kup`, and `bdown` equals `bup`.
- Take a screenshot of Notepad for comparison, then close it with "Don't save".

**M7.4** Screenshots setting. Do these in order:

| step | do | expect |
|---|---|---|
| a | Set `Screenshots while recording` to `start + end`. Record Ctrl+Alt+R, two clicks on an empty spot of the taskbar, Ctrl+Alt+R | Shots = 2 (the start and the end) |
| b | Set it to `none`, and record the same | Shots = 0 |
| c | Set it back to `each click`, and record the same | Shots = 2 (one per click) |
| d | Start a recording | the select and every table button are greyed out until it stops |

- **If Shots is 0 in a or c:** screenshots are failing. Send the console lines that contain `screenshot`.

**M7.5** Rename and delete.

| step | do | expect |
|---|---|---|
| a | Click Rename on a row, type `scroll test`, press Enter | the name changes, and still shows after a page reload |
| b | Click Rename, clear the box, press Enter | a red notice `name must be 1 to 100 characters`. Press Esc: the old name comes back |
| c | Click Delete | the confirm box `Delete "..."? This cannot be undone.` OK removes the row; Cancel keeps it |

Delete the test recordings from M7.2 and M7.4. Keep R1 and the scroll recording.

**M7.6** Ctrl+C in the middle of a recording.

1. Open an empty Notepad. Press Ctrl+Alt+R, click into Notepad and type `saved by ctrl c`.
2. Click into T1 (the server console) and press Ctrl+C once. If T1 is on another screen, switch to it with Alt+Tab instead of clicking: a click there discards the recording.
   - **Expect:** the console shows `recording stopped after ...`, `saved recording N: ...` and the `Saved "...": ...` line, then the normal shutdown lines and `PiComp stopped`.
3. Start `python -m picomp` again.
   - **Expect:** the recording is in the list. In H3 (every step), the last steps are the `bdown 1` / `bup 1` of your click into the console (or the Alt and Tab of Alt+Tab), with **nothing after them**: no `kdown 224` (Ctrl) and no `kdown 6` (c).

**M7.7** *(optional)* Display changes.

- **a.** Open Settings > System > Display on the main screen (a click on another screen would discard the recording for that reason instead). Start a recording, select the **main** display and change its Scale. Only the main display's scale counts.
  - **Expect:** the message `display changed: this recording will be discarded when you stop`.
  - Press Ctrl+Alt+R: the message becomes `Recording discarded: display changed during recording (monitor count, resolution or scaling)`.
  - Put the scale back.
- **b.** With a second screen connected, press Ctrl+Alt+R and click once on the other screen.
  - **Expect:** the message `clicked outside the main screen: this recording will be discarded when you stop`.
  - Press Ctrl+Alt+R: the message becomes `Recording discarded: you clicked or scrolled outside the main screen, which the board cannot reach`.

## M8a. Blind replay

**How every replay works.** Click **Replay**, then take your hand off the mouse and keyboard until it ends. Before each replay, set the scene back the way it was when you recorded: close Notepad, move files back, and so on.

If a replay pauses with `you touched the mouse or keyboard` when you touched nothing, send the console lines that start with `touch detected:`.

**M8a.1** Replay R1. Close Notepad (don't save), then click Replay on R1.

- **Expect while it runs:**
  - the GUI shows `Replaying step i of N: hands off the mouse and keyboard (Ctrl+Alt+Esc stops)`, with i counting up;
  - the LED is green, and orange while a key or button is held;
  - Start opens, `notepad` is typed and Notepad opens; the text is typed, `line` is selected with Shift+Left and replaced by `row`, and `hello` is drag-selected and deleted.
- **Expect at the end:**
  - Notepad shows the same text as at recording time (` world` / `second row`);
  - the GUI is back to `Idle: press Ctrl+Alt+R to record`, with the message `replay finished`;
  - no stray `r`, no menu bar activated by an Alt, and nothing typed into the browser.
- **If not:** send the pause reason or a screenshot of the wrong text, the H3 dump of R1 and the console.

**M8a.2** Scroll the page back to the top, then replay the mouse-wheel browser recording from M7.1.

- **Expect:** the page scrolls the same 5 notches, then `replay finished`.

**M8a.3** Record **R6** in the key log page, then replay it.

1. Open H2 in its own window, and reload it to clear it.
2. Press Ctrl+Alt+R and click inside the page.
3. Slowly type `the quick brown fox` twice (about 10 s). Then press Shift+Right on the laptop keyboard, then Enter.
4. Press Ctrl+Alt+R.
5. Take a screenshot of the page's log.
6. Reload the page (with the reload button), then click Replay on R6.

- **Expect:** after the replay, the log shows the same keys as during the recording (`down KeyT`, `up KeyT`, ..., `down ShiftLeft`, `down ArrowRight`, ..., `down Enter`, `up Enter`), with **no `ControlLeft`, `AltLeft` or `KeyR` lines** at the start or the end.

**M8a.4** GUI Stop. First record **R3** (long typing), which M8b uses too:

1. Open an empty Notepad and leave it where it is.
2. Press Ctrl+Alt+R and click into Notepad.
3. Type `the quick brown fox jumps over the lazy dog` three times at your normal pace, pressing Enter after each (about 20 s).
4. Press Ctrl+Alt+R.

Clear Notepad (Ctrl+A, then Delete). Replay R3, and after a few words move the mouse to **Stop replay** and click it.

- **Expect:**
  - moving the mouse pauses the replay first: `Paused: you touched the mouse or keyboard`, and typing stops;
  - the click on Stop replay ends it: idle, with the message `replay stopped`;
  - nothing is stuck: typing in Notepad works normally.

**M8a.5** Ctrl+Alt+Esc. Reload H2, replay R6, and press Ctrl+Alt+Esc in the middle.

- **Expect:**
  - typing stops, and the GUI shows idle with the message `replay killed (Ctrl+Alt+Esc)`;
  - the page log shows your `down ControlLeft` and `down AltLeft`, but **no `Escape` line**.
- Your Ctrl press pauses the replay a moment before the kill. That is expected.

**M8a.6** BOOT. Clear Notepad, replay R3, and press BOOT in the middle.

- **Expect:**
  - typing stops at once, and the LED turns red;
  - the GUI board line shows `Board halted (BOOT): next Replay re-arms` with a red dot;
  - the state is idle, with the message `board halted (BOOT pressed); next Replay re-arms`.

**M8a.7** Re-arm. Clear Notepad and replay R3 again.

- **Expect:** it runs to the end with `replay finished`. The board line returns to `Board connected (...)`, and the LED is green.

**M8a.8** *(optional)* Display mismatch. Change the **main** display's scale, then click Replay on R1.

- **Expect:** a red notice `screen is WxH at D dpi but the recording was made at WxH at D dpi`.

Put the scale back.

## M8b. Touch detection, Resume and edge cases

**M8b.1** Mouse bump, then Resume. Clear Notepad, replay R3, and nudge the mouse after a few words.

- **Expect:** typing stops. The GUI shows `Paused: you touched the mouse or keyboard`, a **Resume** button with the hint `click Resume, then let go of the mouse`, and **Stop replay**.

Click Resume and let go of the mouse.

- **Expect:**
  - Notepad comes back to the front by itself;
  - the state returns to `Replaying step ...`, and typing continues where it stopped;
  - the replay ends with `replay finished`.
  - One character can appear twice if the pause landed mid-keystroke: Resume re-presses keys that were held.
- **Repeat** the pause and Resume 2 or 3 times in one replay.
- **If it stays at `Paused: could not return to <title>`:** the refocus failed. Send the console lines around `resume refused` and `replay resumed`.
- This step settles open item 7 (an Alt tap plus SetForegroundWindow restores focus).

**M8b.2** Key press, then Stop. Clear Notepad, replay R3, and press Shift on the laptop keyboard.

- **Expect:** `Paused: you touched the mouse or keyboard`.

Click Stop replay.

- **Expect:** idle, with the message `replay stopped`.

**M8b.3** Pause in the middle of a drag in Explorer.

**Set up once:**

1. Create the folder `C:\picomp-test`, with a subfolder `dest` and a text file `drag-me.txt` inside it.
2. Open `C:\picomp-test` in Explorer, in an icon view where both are visible, and leave the window where it is.

**Record R4:**

1. Press Ctrl+Alt+R.
2. Click an empty spot in the Explorer window.
3. Press on `drag-me.txt` and drag it slowly (about 3 s) onto `dest`, then release.
4. Press Ctrl+Alt+R. The file is now in `dest`.

**Reset the scene** (before every R4 replay): move `drag-me.txt` back to `C:\picomp-test`, and make the window look the way it did when you recorded: same folder, same view, same position.

Replay R4, and nudge the mouse while the file is being dragged.

- **Expect:** the drag is cancelled. The file stays in `C:\picomp-test`, and the GUI shows `Paused: you touched the mouse or keyboard`.

Click Resume and let go.

- **Expect:** the pointer goes back to `drag-me.txt`, the drag is redone from its start, and the file lands in `dest`. Then `replay finished`.
- **If the file was dropped halfway at the pause:** send the console, because the Esc rule did not cancel the drag.

**M8b.4** Unplug in the middle of a drag. Reset R4's scene, replay R4, and unplug the board's USB-C cable while the file is being dragged.

- **Expect:**
  - the GUI shows `Board offline`, the state is idle, and the message is `board disconnected`;
  - the console may show `SendInput release: 1 keys/buttons`;
  - when you move your own mouse, nothing is dragged, so no button is stuck, and clicks work normally.
  - The file may have been dropped where the pointer was at the unplug. That is fine.

Plug the board back in.

- **Expect:** `Board connected (...)` within a few seconds, the message clears, and the LED is green.

Reset the scene and replay R4 once more to show that a replay works again.

**M8b.5** Elevated window (the echo check). This needs an administrator account, so that Task Manager runs elevated.

First record **R5**:

1. Place Notepad so it covers the centre of the screen.
2. Press Ctrl+Alt+R.
3. Click the centre of the screen, inside Notepad.
4. Type `abc`.
5. Press Ctrl+Alt+R.

- **a. Control run.** Clear Notepad and replay R5.
  - **Expect:** `abc` is typed, then `replay finished`.
- **b. Elevated run.**
  1. Open Task Manager (Ctrl+Shift+Esc).
  2. If its settings offer "Always on top", turn it on.
  3. Move Task Manager so it covers the centre of the screen. The GUI's Replay button must stay visible, and the browser window must not cover the centre once you click it.
  4. Replay R5.
  - **Expect:** the replay's click lands on Task Manager, and within about half a second the GUI shows `Paused: input hooks are not seeing the replay (admin window focused, secure desktop, or hook dropped)`. A typed letter may select a process in Task Manager's list, which is harmless.
  5. Click Stop replay and close Task Manager.
- **If it finishes instead:** check whether Task Manager is elevated (Details tab, add the "Elevated" column), and tell me.
- This step settles open item 8 (hooks are blind over elevated windows, and the echo check catches it).

**M8b.6** UAC prompt (the secure desktop). First record **R8**:

1. Open an empty Notepad.
2. Press Ctrl+Alt+R.
3. Press Win+R, type `cmd` and press Ctrl+Shift+Enter.
4. When the UAC prompt appears, click **No**.
5. Click into Notepad and type `after`.
6. Press Ctrl+Alt+R.

- **Check with H3:** the steps include `kdown 227` (Win), `kdown 21` (R), the letters 6, 16 and 7 (`cmd`), then `kdown 224`, `kdown 225` and `kdown 40`, and **a `kup` for each of 224, 225 and 40**. If you were still holding them when the prompt appeared, the console shows `key-up of usage ... not seen by the hook; inserted at ... ms`.

Clear Notepad and replay R8.

- **Expect:**
  1. The Run dialog opens, `cmd` is typed, and the UAC prompt appears.
  2. Answer it yourself with **No**.
  3. The GUI then shows `Paused: secure desktop`, or the echo reason if your UAC setting does not dim the screen.
  4. Click Stop replay.
  5. Type a few letters in Notepad. They come out lowercase and no shortcuts fire, which means nothing is stuck.

**M8b.7** *(optional)* Extra edge cases.

- **a. Refocus failure.**
  1. Replay R3, and nudge the mouse to pause it.
  2. Close Notepad (don't save).
  3. Click Resume.
  - **Expect:** it stays paused with `Paused: could not return to the target window`.
  4. Click Stop replay.
- **b. Display change during a replay.**
  1. Replay R3, then change the **main** display's scale.
     - **Expect:** `Paused: you touched the mouse or keyboard`, because reaching Settings moves the mouse first. A display change only pauses a replay that is still running, so this message stays.
  2. Click Resume.
     - **Expect:** a red notice `cannot resume: display changed (monitor count, resolution or scaling)`.
  3. Put the scale back.
     - **Expect:** Resume now works.
- **c. Server crash during a replay.**
  1. In T2 (PowerShell), start this timer:

     ```
     Start-Sleep 10; Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*-m picomp*' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
     ```

  2. Clear Notepad and replay R3.
  - **Expect:** when the server dies, typing stops at once (LED blue) or within 1 s (LED green), and no key keeps repeating.
  3. Start the server again.
- **d. Ctrl+C during a replay.**
  1. Replay R3, then click into T1. That click pauses the replay.
  2. Press Ctrl+C.
  - **Expect:** a clean exit, and nothing stuck.

## Open items (PLAN.md section 14)

| item | settled at | passes when | if it fails |
|---|---|---|---|
| 1. Composite device with class 0xEF/02/01 gives both COM ports | M2.4, M2.5 | two COM ports, a keyboard and a mouse, with no warnings | send the Device Manager details; the descriptors get checked before anything else |
| 2. Windows maps 0..32767 onto the full screen | M3a.1 | `all corners ok` | send the output; `win.py` gets adjusted |
| 3. `rp2.bootsel_button()` works | M1 (done), plus M4.4 with USB busy | `!HALT` and a red LED | the BOOT kill is dropped |
| 4. DTR drops when the server process dies | M4.2 (also M8b.7c) | the LED turns blue at once | the 1 s heartbeat is the backstop, so this still passes; tell me which |
| 5. Board keys arrive as injected=False; auto-repeat and fake shifts are visible to hooks | M3b.1-M3b.3 | `0 usages flagged injected`, a rising hold count, and the Shift+Right listing | touch rules and `keymap.py` get adjusted from your output |
| 6. Hooks capture touchpad two-finger scrolling | M7.1 | wheel steps from the touchpad | the README keeps "scroll with a mouse wheel while recording" |
| 7. An Alt tap plus SetForegroundWindow restores focus | M8b.1 | Notepad returns and typing continues | the AttachThreadInput fallback is already in `win.set_foreground`; send the logs |
| 8. Hooks are blind over elevated windows | M8b.5 | the echo-check pause | it is a known limitation; tell me what happened |

## Reporting and wrap-up

**1. Send me one summary** with:

- pass or fail for each milestone;
- the two keyscan outputs (M3b.1, M3b.2);
- the M4.2 result (blue or green);
- the M7.1 messages and counts;
- the M8b.1 and M8b.5 results;
- the `pip freeze` output (M6.7);
- the output for anything that failed.

**2. Expect follow-up edits** from your results:

- `_PAN_SIGN`, if you flipped it;
- `keymap.py` corrections;
- the README's touchpad line;
- the exact versions in `environment.yml`.

**3. Commit** when you are happy (or ask me to do it):

```
git status
git add README.md TESTPLAN.md pytest.ini firmware picomp tests tools
git commit -m "M2-M8: firmware, server, GUI, fwcheck, tests"
```

Before you commit, check that `git status` lists no `data/` and no `__pycache__/`. Both are ignored.

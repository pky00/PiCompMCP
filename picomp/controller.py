# The core: one state machine (idle, recording, saving, replaying, paused). Every
# action the GUI, the hotkeys or REST (and later MCP) can take is a method here.
#
# Threads that call in: HTTP endpoints, the dispatcher (hook events and the 250 ms
# tick), the board reader (on_board_event, ping_ok) and the replay thread (callbacks).
# Lock rules (PLAN.md section 5):
#   1. A stop reaches the player (flag, Esc rule, R) before the state is updated
#      under the controller lock; only a brief read of _player comes first.
#   2. The controller lock guards in-memory state only. It is never held during
#      SQLite, serial writes, hook restarts, player calls or waits on other threads.
#   3. Board callbacks arrive without any BoardLink lock held.
import logging
import queue
import threading
import time

from picomp import board, db, hooks, player, recorder, win

log = logging.getLogger(__name__)

STATES = ("idle", "recording", "saving", "replaying", "paused")

REASON_DISPLAY = "display changed (monitor count, resolution or scaling)"
REASON_STOPPED = "replay stopped"
REASON_KILLED = "replay killed (Ctrl+Alt+Esc)"
REASON_HALT = "board halted (BOOT pressed); next Replay re-arms"
REASON_TIMEOUT = "board heartbeat timeout; the board released everything"
REASON_BOARD_ERR = "board error: {}"
REASON_SHUTDOWN = "server shutting down"

DISCARD_DISPLAY = "display changed during recording (monitor count, resolution or scaling)"
WARN_DISPLAY = "display changed: this recording will be discarded when you stop"
WARN_ELSEWHERE = "clicked outside the main screen: this recording will be discarded when you stop"

TICK_NS = 250_000_000
ALIVE_NS = 1_000_000_000  # liveness stamps older than this stop the board PINGs
SHOT_DRAIN_S = 10.0
SHUTDOWN_SAVE_S = 15.0

_REPLAY_STATES = ("replaying", "paused")
_SAVE_STATES = ("recording", "saving")

# board event -> (stop reason, how the player stops)
_BOARD_STOPS = {
    "halt": (REASON_HALT, "released"),
    "timeout": (REASON_TIMEOUT, "released"),
    "lost": (player.REASON_LOST, "lost"),
}


class Refused(Exception):
    pass


class NotFound(Exception):
    pass


def _recording_meta(t0_ns, display, shots_mode):
    # t0 is a perf_counter stamp (the R key-down); turn it into wall-clock time.
    wall = time.time() - max(0, time.perf_counter_ns() - t0_ns) / 1e9
    return {
        "name": time.strftime("Recording %Y-%m-%d %H:%M:%S", time.localtime(wall)),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall)),
        "screen_w": display[0],
        "screen_h": display[1],
        "dpi": display[2],
        "shots_mode": shots_mode,
    }


class Controller:
    def __init__(self, db_path=None):
        self.db = db.Db(db_path or db.DB_PATH)
        self.board = board.BoardLink(on_event=self.on_board_event, ping_ok=self.ping_ok)
        self._events = queue.SimpleQueue()
        self.hooks = hooks.Hooks(self._events.put)
        self.held = hooks.HeldModel()
        self.shots = recorder.ShotWorker()
        self._lock = threading.RLock()
        self._state = "idle"
        self._message = ""
        self._settings = {"screenshots": db.SHOTS_MODES[0]}
        self._recorder = None  # set and cleared on the dispatcher thread only
        self._rec_meta = None  # the recordings row being built
        self._rec_t0_ns = 0
        self._player = None
        self._replay = None  # {"id", "name", "step", "total"} while replaying or paused
        self._replay_display = None
        self._start_cancel = None  # (reason, how) of a stop that came while start_replay was setting up
        self._dispatcher = None
        self._closing = False
        self._dispatch_alive_ns = time.perf_counter_ns()

    # --- lifecycle (uvicorn lifespan, run in the threadpool) ---

    def startup(self):
        self.db.init()
        settings = self.db.get_settings()
        with self._lock:
            self._settings = settings
        self.shots.start()
        self.hooks.start()
        self._closing = False
        self._dispatch_alive_ns = time.perf_counter_ns()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="dispatcher", daemon=True)
        self._dispatcher.start()
        self.board.start()
        log.info("controller started (database %s, screenshots %s)", self.db.path, settings["screenshots"])

    def shutdown(self):
        p = self._player
        self.stop_replay(REASON_SHUTDOWN)
        if p is not None:
            p.join(2)
        self._save_for_shutdown()
        for what, stop in (("input hooks", self.hooks.stop), ("dispatcher", self._stop_dispatcher),
                           ("screenshot worker", self.shots.stop), ("board link", self.board.close)):
            try:
                stop()
            except Exception:
                log.exception("shutdown: stopping the %s failed", what)
        log.info("controller stopped")

    def _save_for_shutdown(self):
        with self._lock:
            state = self._state
        if state not in _SAVE_STATES:
            return
        if self._dispatcher is None or not self._dispatcher.is_alive():
            # Nobody else can touch the recorder now, so finish it on this thread.
            self.stop_recording(time.perf_counter_ns())
            return
        if state == "recording":
            # Through the queue, so the Ctrl+C keystrokes already queued reach the
            # recorder before the cut that removes them.
            self._events.put(hooks.hotkey_ev(time.perf_counter_ns(), "shutdown"))
        deadline = time.perf_counter() + SHUTDOWN_SAVE_S
        while time.perf_counter() < deadline:
            with self._lock:
                if self._state not in _SAVE_STATES:
                    return
            time.sleep(0.05)
        log.warning("the recording was still being saved after %.0f s; it may be lost", SHUTDOWN_SAVE_S)

    def _stop_dispatcher(self):
        self._closing = True
        if self._dispatcher is not None:
            self._dispatcher.join(1)
            if self._dispatcher.is_alive():
                log.warning("dispatcher did not stop within 1 s")

    # --- status ---

    def status(self):
        board_status, board_detail = self.board.status, self.board.detail
        with self._lock:
            rec, p = self._recorder, self._player
            replay = dict(self._replay) if self._replay else None
            st = {
                "board": board_status,
                "board_detail": board_detail,
                "state": self._state,
                "replay": replay,
                "recording": None,
                "message": self._message,
            }
        if replay is not None and p is not None:
            replay["step"] = p.step
        if rec is not None:
            st["recording"] = {"steps": rec.step_count}
        return st

    def _refuse(self, text):
        with self._lock:
            self._message = text
        raise Refused(text)

    def _require_idle(self):
        with self._lock:
            if self._state != "idle":
                self._refuse("not allowed while %s" % self._state)

    # --- recording (dispatcher thread) ---

    def start_recording(self, t0_ns):
        d = win.display()
        with self._lock:
            if self._state != "idle":
                self._refuse("cannot record while %s" % self._state)
            mode = self._settings["screenshots"]
            self.shots.begin()
            self._recorder = recorder.Recorder(t0_ns, d, mode, request_shot=self.shots.request)
            self._rec_meta = _recording_meta(t0_ns, d, mode)
            self._rec_t0_ns = t0_ns
            self._state = "recording"
            self._message = ""
        log.info("recording started (main screen %dx%d at %d dpi, %d screens, screenshots %s)",
                 d[0], d[1], d[2], len(d[3]), mode)

    def stop_recording(self, stop_ns):
        with self._lock:
            if self._state != "recording":
                return
            rec, meta = self._recorder, self._rec_meta
            self._recorder = self._rec_meta = None
            self._state = "saving"
        log.info("recording stopped after %.1f s; saving", (stop_ns - self._rec_t0_ns) / 1e9)
        message = ""
        try:
            message = self._finish_recording(rec, meta)
        except Exception as e:
            log.exception("saving the recording failed")
            message = "Recording failed to save: %s" % e
        finally:
            with self._lock:
                self._state = "idle"
                self._message = message
        log.info("%s", message)

    def _finish_recording(self, rec, meta):
        result = rec.finish()
        shots = self.shots.drain(SHOT_DRAIN_S)
        if result.discard_reason:
            return "Recording discarded: " + result.discard_reason
        steps = result.steps
        if not steps:
            return "Recording discarded: no steps"
        kept = recorder.final_shots(rec.shots_mode, shots, len(steps))
        self.db.save_recording(dict(meta, duration_ms=steps[-1].t_ms), steps, kept)
        text = 'Saved "%s": %d steps, %d shots, %d software-injected inputs ignored' % (
            meta["name"], len(steps), len(kept), result.injected)
        if result.unmapped:
            text += ", %d unmapped keys dropped" % result.unmapped
        return text

    # --- recordings ---

    def list_recordings(self):
        return self.db.list_recordings()

    def rename_recording(self, rec_id, name):
        self._require_idle()
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("name must be 1 to 100 characters")
        if not self.db.rename_recording(rec_id, name):
            raise NotFound("no recording %s" % rec_id)
        return {"id": rec_id, "name": name}

    def delete_recording(self, rec_id):
        self._require_idle()
        if not self.db.delete_recording(rec_id):
            raise NotFound("no recording %s" % rec_id)

    # --- replay ---

    def start_replay(self, rec_id):
        info = self.db.get_recording(rec_id)
        if info is None:
            raise NotFound("no recording %s" % rec_id)
        online = self.board.online
        d = win.display()
        w, h, dpi = d[:3]  # the main screen, the only one the board reaches
        recorded = (info["screen_w"], info["screen_h"], info["dpi"])
        with self._lock:
            if self._state != "idle":
                self._refuse("cannot replay while %s" % self._state)
            if not online:
                self._refuse("board is not connected")
            if recorded != (w, h, dpi):
                self._refuse("screen is %dx%d at %d dpi but the recording was made at %dx%d at %d dpi"
                             % ((w, h, dpi) + recorded))
            self._state = "replaying"
            self._replay = {"id": info["id"], "name": info["name"], "step": 0, "total": info["steps"]}
            self._replay_display = d
            self._start_cancel = None
            self._message = ""
        try:
            p = self._launch_player(info["id"], (w, h))
        except Exception as e:
            if isinstance(e, Refused):
                text = str(e)
            else:
                log.exception("replay of recording %s failed to start", rec_id)
                text = "replay failed to start: %s" % e
            with self._lock:
                self._player = self._replay = self._replay_display = None
                self._state = "idle"
                self._message = text
            raise Refused(text) from None
        log.info("replaying %r: %d steps", info["name"], p.total)
        return {"id": info["id"], "total": p.total}

    def _launch_player(self, rec_id, screen):
        # Fresh listeners: Windows can drop a hook silently, and the echo check
        # would otherwise pause every replay.
        self.hooks.restart()
        # HELLO re-arms the board after a BOOT halt and releases everything.
        if not self.board.hello():
            raise Refused("board did not answer HELLO")
        steps = self.db.load_steps(rec_id)
        if not steps:
            raise Refused("recording has no steps")
        p = player.Player(steps, screen, self.board.send,
                          held_snapshot=self.held.snapshot,
                          dispatcher_alive_ns=lambda: self._dispatch_alive_ns,
                          on_pause=self._on_player_pause, on_end=self._finish_replay)
        with self._lock:
            cancel = self._start_cancel
            if cancel is None:
                self._player = p
                self._replay["total"] = p.total
        if cancel is not None:
            raise Refused(cancel[0])
        # A stop from here on reaches the player; one that lands before begin()
        # leaves it "done", and begin() then refuses to start it.
        p.start()
        return p

    def resume_replay(self):
        d = win.display()
        with self._lock:
            p = self._player
            if self._state != "paused" or p is None:
                self._refuse("nothing to resume")
            if d != self._replay_display:
                self._refuse("cannot resume: " + REASON_DISPLAY)
        try:
            self.hooks.restart()
        except Exception as e:
            log.exception("restarting the input hooks failed")
            self._refuse("cannot resume: the input hooks failed to restart (%s)" % e)
        r = p.resume()
        with self._lock:
            if self._player is not p:
                return  # stopped meanwhile; _finish_replay already set the state
            if r is None:
                # It may already have paused again (on_pause sets that state).
                if p.phase != "paused":
                    self._state = "replaying"
                    self._message = ""
            elif p.phase == "paused":
                self._message = r  # refocus failed: still paused, the status says why

    def stop_replay(self, reason=REASON_STOPPED, how="normal"):
        with self._lock:
            p = self._player
            if p is None:
                # start_replay is still restarting the hooks or sending HELLO; it
                # applies this stop before it creates the player.
                if self._state == "replaying" and self._start_cancel is None:
                    self._start_cancel = (reason, how)
                return
        try:
            p.stop(reason, how)
        except Exception:
            log.exception("stopping the replay failed")
        self._finish_replay(p, reason)

    def kill(self):
        self.stop_replay(REASON_KILLED)

    def _on_player_pause(self, p, reason):
        with self._lock:
            if self._player is p and p.phase == "paused":
                self._state = "paused"
                self._message = reason

    def _finish_replay(self, p, reason):
        # Called by stop_replay and by the replay thread as it exits; the first wins.
        with self._lock:
            if self._player is not p:
                return
            self._player = self._replay = self._replay_display = None
            self._state = "idle"
            self._message = reason or ""
        log.info("replay ended: %s", reason)

    # --- settings ---

    def get_settings(self):
        with self._lock:
            return dict(self._settings)

    def set_settings(self, values):
        self._require_idle()
        mode = values.get("screenshots")
        if mode not in db.SHOTS_MODES:
            raise ValueError("screenshots must be one of " + ", ".join(db.SHOTS_MODES))
        self.db.set_setting("screenshots", mode)
        with self._lock:
            self._settings["screenshots"] = mode
            return dict(self._settings)

    # --- board reader thread ---

    def on_board_event(self, kind, line):
        if kind == "connected":
            with self._lock:
                if self._message == player.REASON_LOST:
                    self._message = ""
            return
        if kind == "err":
            reason, how = REASON_BOARD_ERR.format(line), "normal"
        elif kind in _BOARD_STOPS:
            reason, how = _BOARD_STOPS[kind]
        else:
            log.warning("unknown board event %r %r", kind, line)
            return
        with self._lock:
            replaying = self._state in _REPLAY_STATES
            # A command already in flight when BOOT stopped the replay comes back as
            # "ERR <cmd> halted"; the halt message says more, so keep it.
            if not replaying and not (kind == "err" and line.endswith(" halted")):
                self._message = reason
        if replaying:
            self.stop_replay(reason, how)

    def ping_ok(self):
        # No lock: the board reader must never wait on the controller. A hung
        # dispatcher or replay thread stops the PINGs, so the board releases after 1 s.
        p = self._player
        if p is None or p.phase not in ("running", "paused", "resuming"):
            return True
        now = time.perf_counter_ns()
        return now - self._dispatch_alive_ns <= ALIVE_NS and now - p.alive_ns <= ALIVE_NS

    # --- dispatcher thread ---

    def _dispatch_loop(self):
        last_tick = time.perf_counter_ns()
        while not self._closing:
            try:
                ev = self._events.get(timeout=TICK_NS / 1e9)  # a wake-up, not a timer
            except queue.Empty:
                ev = None
            now = time.perf_counter_ns()
            self._dispatch_alive_ns = now
            if ev is not None:
                try:
                    self.handle_event(ev)
                except Exception:
                    log.exception("handling a %s event failed", ev.kind)
            if now - last_tick >= TICK_NS:
                last_tick = now
                try:
                    self.tick(now)
                except Exception:
                    log.exception("dispatcher tick failed")

    def handle_event(self, ev):
        if ev.kind == "hotkey":
            self._hotkey(ev)
            return
        self.held.update(ev)
        rec = self._recorder
        if rec is not None:
            rec.feed(ev)
            self._warn_if_elsewhere(rec)
        p = self._player
        if p is not None:
            p.observe(ev)

    def _hotkey(self, ev):
        with self._lock:
            state = self._state
        if ev.hotkey == "record":
            # In other states the chord's Ctrl and Alt are touches, which pause a replay.
            if state == "idle":
                try:
                    self.start_recording(ev.t_ns)
                except Refused as e:
                    log.info("recording refused: %s", e)
            elif state == "recording":
                self.stop_recording(ev.t_ns)
        elif ev.hotkey == "kill":
            if state in _REPLAY_STATES:
                self.kill()
        elif ev.hotkey == "shutdown":
            if state == "recording":
                self.stop_recording(ev.t_ns)

    def _warn_if_elsewhere(self, rec):
        if rec.discard_reason == recorder.DISCARD_ELSEWHERE and self._message != WARN_ELSEWHERE:
            with self._lock:
                self._message = WARN_ELSEWHERE

    def tick(self, now_ns):
        self.held.reconcile(now_ns)
        rec = self._recorder
        if rec is not None:
            rec.tick(now_ns)
            self._warn_if_elsewhere(rec)
        d = win.display()
        if rec is not None and rec.discard_reason is None and d != rec.display:
            log.warning("display changed during the recording: %s -> %s", rec.display, d)
            rec.discard(DISCARD_DISPLAY)
            with self._lock:
                self._message = WARN_DISPLAY
        p = self._player
        if p is not None and p.phase == "running" and d != self._replay_display:
            log.warning("display changed during the replay: %s -> %s", self._replay_display, d)
            p.pause(REASON_DISPLAY)

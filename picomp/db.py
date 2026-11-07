# SQLite storage (PLAN.md section 8): recordings with their steps and screenshots,
# plus settings. One short-lived connection per call, so any thread may use a Db.
import collections
import contextlib
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "picomp.db"

SHOTS_MODES = ("each_click", "start_end", "none")

SCHEMA = """
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
"""

Step = collections.namedtuple("Step", "seq t_ms kind x y code wheel_v wheel_h",
                              defaults=(None, None, None, None, None))

_SELECT_RECORDINGS = """
SELECT r.id, r.name, r.created_at, r.screen_w, r.screen_h, r.dpi, r.duration_ms, r.shots_mode,
       (SELECT COUNT(*) FROM steps s WHERE s.recording_id = r.id) AS steps,
       (SELECT COUNT(*) FROM screenshots p WHERE p.recording_id = r.id) AS shots
FROM recordings r
"""

_INSERT_RECORDING = """
INSERT INTO recordings (name, created_at, screen_w, screen_h, dpi, duration_ms, shots_mode)
VALUES (:name, :created_at, :screen_w, :screen_h, :dpi, :duration_ms, :shots_mode)
"""
_RECORDING_FIELDS = ("name", "created_at", "screen_w", "screen_h", "dpi", "duration_ms", "shots_mode")

_INSERT_STEP = """
INSERT INTO steps (recording_id, seq, t_ms, kind, x, y, code, wheel_v, wheel_h)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class Db:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)  # default 5 s busy timeout
        try:
            conn.execute("PRAGMA foreign_keys=ON")  # per connection: needed for the cascades
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Persistent in the file, so once at startup is enough.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            log.warning("SQLite refused WAL mode (journal_mode is %s)", mode)

    def journal_mode(self):
        with self._conn() as conn:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]

    def save_recording(self, rec, steps, shots):
        # One transaction: the recording, its steps and its screenshots, or nothing.
        with self._conn() as conn:
            cur = conn.execute(_INSERT_RECORDING, {k: rec[k] for k in _RECORDING_FIELDS})
            rec_id = cur.lastrowid
            conn.executemany(_INSERT_STEP, [(rec_id,) + tuple(s) for s in steps])
            conn.executemany("INSERT INTO screenshots (recording_id, seq, png) VALUES (?, ?, ?)",
                             [(rec_id, seq, png) for seq, png in sorted(shots.items())])
        log.info("saved recording %d: %d steps, %d screenshots", rec_id, len(steps), len(shots))
        return rec_id

    def list_recordings(self):
        with self._conn() as conn:
            rows = conn.execute(_SELECT_RECORDINGS + "ORDER BY r.id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_recording(self, rec_id):
        with self._conn() as conn:
            row = conn.execute(_SELECT_RECORDINGS + "WHERE r.id = ?", (rec_id,)).fetchone()
        return dict(row) if row else None

    def load_steps(self, rec_id):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, t_ms, kind, x, y, code, wheel_v, wheel_h FROM steps"
                " WHERE recording_id = ? ORDER BY seq", (rec_id,)).fetchall()
        return [Step(*r) for r in rows]

    def rename_recording(self, rec_id, name):
        with self._conn() as conn:
            cur = conn.execute("UPDATE recordings SET name = ? WHERE id = ?", (name, rec_id))
        return cur.rowcount > 0

    def delete_recording(self, rec_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
        return cur.rowcount > 0

    def get_settings(self):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'screenshots'").fetchone()
        mode = row[0] if row else None
        if mode not in SHOTS_MODES:
            log.warning("unknown screenshots setting %r; using %s", mode, SHOTS_MODES[0])
            mode = SHOTS_MODES[0]
        return {"screenshots": mode}

    def set_setting(self, key, value):
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

# picomp/db.py on a temp file: schema, WAL, settings, save/list/load, rename,
# cascade deletes and the one-transaction save (PLAN.md section 8).
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from picomp import db
from picomp.db import Db, Step

REC = {
    "name": "Recording 2026-10-21 14:02:11",
    "created_at": "2026-10-21T11:02:11Z",
    "screen_w": 1920,
    "screen_h": 1080,
    "dpi": 120,
    "duration_ms": 8450,
    "shots_mode": "each_click",
}

STEPS = [
    Step(0, 0, "move", 10, 20),
    Step(1, 16, "bdown", 10, 20, 1),
    Step(2, 90, "bup", 12, 20, 1),
    Step(3, 120, "wheel", 12, 20, wheel_v=-2, wheel_h=0),
    Step(4, 300, "kdown", code=4),
    Step(5, 8450, "kup", code=4),
]

SHOTS = {1: b"\x89PNG one", 3: b"\x89PNG three"}


def rows(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, args).fetchall()


@pytest.fixture
def store(tmp_path):
    d = Db(tmp_path / "data" / "picomp.db")
    d.init()
    return d


def test_constructor_does_no_io(tmp_path):
    path = tmp_path / "nowhere" / "picomp.db"
    assert Db(path).path == path
    assert not path.parent.exists()


def test_default_path_is_in_the_repo_data_folder():
    root = Path(db.__file__).resolve().parent.parent
    assert db.DB_PATH == root / "data" / "picomp.db"


def test_init_creates_the_schema(store):
    tables = rows(store.path, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    assert tables == [("recordings",), ("screenshots",), ("settings",), ("steps",)]
    assert rows(store.path, "PRAGMA user_version") == [(1,)]


def test_wal_is_on(store):
    assert store.journal_mode() == "wal"
    assert rows(store.path, "PRAGMA journal_mode") == [("wal",)]


def test_default_settings(store):
    assert db.SHOTS_MODES == ("each_click", "start_end", "none")
    assert store.get_settings() == {"screenshots": "each_click"}


@pytest.mark.parametrize("mode", db.SHOTS_MODES)
def test_settings_round_trip(store, mode):
    store.set_setting("screenshots", mode)
    assert store.get_settings() == {"screenshots": mode}


def test_unknown_stored_setting_falls_back_to_each_click(store):
    store.set_setting("screenshots", "sometimes")
    assert store.get_settings() == {"screenshots": "each_click"}


def test_init_again_keeps_data_and_settings(store):
    rec_id = store.save_recording(REC, STEPS, SHOTS)
    store.set_setting("screenshots", "none")
    store.init()
    assert store.get_settings() == {"screenshots": "none"}
    assert [r["id"] for r in store.list_recordings()] == [rec_id]


def test_save_and_list(store):
    rec_id = store.save_recording(dict(REC), STEPS, SHOTS)
    assert isinstance(rec_id, int)
    assert store.list_recordings() == [dict(REC, id=rec_id, steps=6, shots=2)]


def test_get_recording(store):
    rec_id = store.save_recording(REC, STEPS, SHOTS)
    assert store.get_recording(rec_id) == dict(REC, id=rec_id, steps=6, shots=2)
    assert store.get_recording(rec_id + 1) is None


def test_extra_recording_keys_are_ignored(store):
    rec_id = store.save_recording(dict(REC, extra="ignored", steps=99), STEPS, {})
    assert store.get_recording(rec_id)["steps"] == 6


def test_list_is_newest_first(store):
    first = store.save_recording(REC, STEPS, {})
    second = store.save_recording(dict(REC, name="second"), STEPS[:2], {})
    listed = store.list_recordings()
    assert [r["id"] for r in listed] == [second, first]
    assert [(r["name"], r["steps"], r["shots"]) for r in listed] == [("second", 2, 0), (REC["name"], 6, 0)]


def test_load_steps_in_seq_order(store):
    rec_id = store.save_recording(REC, list(reversed(STEPS)), {})
    loaded = store.load_steps(rec_id)
    assert loaded == STEPS
    assert all(type(s) is Step for s in loaded)


def test_load_steps_of_unknown_recording(store):
    assert store.load_steps(42) == []


def test_screenshots_are_stored_as_blobs(store):
    rec_id = store.save_recording(REC, STEPS, SHOTS)
    stored = rows(store.path, "SELECT seq, png FROM screenshots WHERE recording_id = ? ORDER BY seq", (rec_id,))
    assert stored == [(1, b"\x89PNG one"), (3, b"\x89PNG three")]


def test_start_end_shots_at_zero_and_n(store):
    rec = dict(REC, shots_mode="start_end")
    rec_id = store.save_recording(rec, STEPS, {0: b"start", len(STEPS): b"end"})
    stored = rows(store.path, "SELECT seq FROM screenshots WHERE recording_id = ? ORDER BY seq", (rec_id,))
    assert stored == [(0,), (6,)]
    assert store.get_recording(rec_id)["shots"] == 2


def test_rename(store):
    rec_id = store.save_recording(REC, STEPS, {})
    assert store.rename_recording(rec_id, "Login flow") is True
    assert store.get_recording(rec_id)["name"] == "Login flow"
    assert store.rename_recording(rec_id + 1, "nope") is False


def test_delete_cascades_to_steps_and_screenshots(store):
    gone = store.save_recording(REC, STEPS, SHOTS)
    kept = store.save_recording(REC, STEPS[:3], {1: b"kept"})
    assert store.delete_recording(gone) is True
    for table in ("steps", "screenshots"):
        assert rows(store.path, "SELECT COUNT(*) FROM %s WHERE recording_id = ?" % table, (gone,)) == [(0,)]
    assert [r["id"] for r in store.list_recordings()] == [kept]
    assert store.get_recording(kept)["steps"] == 3
    assert store.get_recording(kept)["shots"] == 1
    assert store.delete_recording(gone) is False


@pytest.mark.parametrize("steps", [
    STEPS[:2] + [Step(2, 99, "teleport", 0, 0)],
    STEPS[:2] + [Step(1, 99, "move", 0, 0)],
])
def test_save_is_one_transaction(store, steps):
    with pytest.raises(sqlite3.IntegrityError):
        store.save_recording(REC, steps, SHOTS)
    assert store.list_recordings() == []
    assert rows(store.path, "SELECT COUNT(*) FROM steps") == [(0,)]
    assert rows(store.path, "SELECT COUNT(*) FROM screenshots") == [(0,)]


def test_bad_shots_mode_is_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.save_recording(dict(REC, shots_mode="sometimes"), STEPS, {})
    assert store.list_recordings() == []


def test_step_defaults():
    assert Step(4, 300, "kdown", code=4) == (4, 300, "kdown", None, None, 4, None, None)
    assert Step(0, 0, "move", 1, 2)._fields == ("seq", "t_ms", "kind", "x", "y", "code", "wheel_v", "wheel_h")

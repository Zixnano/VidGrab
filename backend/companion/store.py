"""Companion persistence.

The companion keeps its own SQLite file (companion.db) next to the downloader's database, so a
pet write can never wait on the downloader's locks and removing the feature removes one file.
Tables are versioned and migrated additively. Legacy v1/v2 data that an earlier patch put in the
downloader's database is copied over once, never deleted.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import diag

SCHEMA_VERSION = 3
REGISTRY_VERSION = 3                  # the species.json "version" this build understands
EVENT_KEEP = 500
_PRUNE_EVERY = 25

_LEGACY_TABLES = ("companion_state", "companion_inventory", "companion_unlocks",
                  "stickers", "companion_events", "companion_daily_rewards")

_REGISTRY_CACHE = {"mtime": None, "data": {}}


def registry_path():
    return Path(__file__).resolve().parent / "species.json"


def load_registry():
    """species.json, cached by modification time (the file is read once, not every frame)."""
    path = registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _REGISTRY_CACHE["mtime"] == mtime and _REGISTRY_CACHE["data"]:
        return _REGISTRY_CACHE["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            if int(data.get("version", REGISTRY_VERSION)) > REGISTRY_VERSION:
                diag.note("registry_version", "species.json is version %s, this build understands up to %d; "
                          "reading it anyway" % (data.get("version"), REGISTRY_VERSION))
        except (TypeError, ValueError, AttributeError):
            pass
    except Exception as exc:
        diag.note("registry", "species.json unreadable, using the built-in puppy-only fallback", exc)
        data = {"version": 3, "frame_size": 64, "animation": {}, "egg": {}, "species": [
            {"id": "puppy", "name": "Mochi", "display_name": "Lavender Puppy", "sheet": "puppy.png",
             "columns": 4, "rows": 2, "unlocked_by_default": True, "unlock": None, "blink": None,
             "frames": {"idle": [0]}}]}
    _REGISTRY_CACHE["mtime"], _REGISTRY_CACHE["data"] = mtime, data
    return data


class CompanionStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._event_inserts = 0
        self._init()

    # ---- connection --------------------------------------------------
    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _init(self):
        with self._lock:
            c0 = sqlite3.connect(self.db_path, timeout=10)
            try:
                c0.execute("PRAGMA journal_mode=WAL")
                c0.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass
            finally:
                c0.close()
        with self._lock, self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS companion_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS companion_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_ts REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS companion_inventory (item_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS companion_unlocks (unlock_id TEXT PRIMARY KEY, unlocked_ts REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS stickers (sticker_id TEXT PRIMARY KEY, filename TEXT NOT NULL, manifest TEXT NOT NULL, created_ts REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS companion_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, payload TEXT NOT NULL, created_ts REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS companion_daily_rewards (day TEXT PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0)")
            # v3: the download dedupe memory has its own table. In v2 it lived inside the 500-row
            # event log, so ordinary care actions pushed old downloads out and re-downloads paid again.
            c.execute("CREATE TABLE IF NOT EXISTS companion_downloads (fingerprint TEXT PRIMARY KEY, job_id TEXT NOT NULL DEFAULT '', xp INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL)")
            row = c.execute("SELECT value FROM companion_meta WHERE key='schema_version'").fetchone()
            old = int(row[0]) if row else 0
            if old > SCHEMA_VERSION:
                raise RuntimeError("Companion data was created by a newer version")
            if 0 < old < 3:
                self._backfill_downloads(c)
            c.execute("INSERT OR REPLACE INTO companion_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            c.execute("INSERT OR IGNORE INTO companion_state(id,data,updated_ts) VALUES(1,?,?)", ("{}", time.time()))

    @staticmethod
    def _backfill_downloads(c):
        try:
            c.execute(
                "INSERT OR IGNORE INTO companion_downloads(fingerprint,job_id,xp,ts) "
                "SELECT json_extract(payload,'$.fingerprint'), COALESCE(json_extract(payload,'$.job_id'),''), "
                "COALESCE(json_extract(payload,'$.xp'),0), created_ts FROM companion_events "
                "WHERE event='download' AND json_extract(payload,'$.fingerprint') IS NOT NULL")
        except sqlite3.DatabaseError:
            pass

    # ---- one-time import of data an earlier patch kept in the main DB ----
    def import_legacy(self, main_db_path):
        """Copy companion tables out of the downloader's database if this store is still empty.
        Non-destructive: the source tables are left alone. Returns True if anything was copied."""
        main_db_path = str(main_db_path)
        if main_db_path == self.db_path or not Path(main_db_path).is_file():
            return False
        with self._lock:
            if self.get_meta("legacy_imported") == "1":
                return False
            if self.load_state():
                self.set_meta("legacy_imported", "1")
                return False
            copied = False
            try:
                src = sqlite3.connect(Path(main_db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            except sqlite3.DatabaseError as exc:
                diag.note("legacy_open", "could not open the downloads database to look for older companion data", exc)
                return False
            try:
                have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                row = src.execute("SELECT data FROM companion_state WHERE id=1").fetchone() if "companion_state" in have else None
                if not row or not json.loads(row[0] or "{}"):
                    self.set_meta("legacy_imported", "1")
                    return False
                with self._conn() as c:
                    for table in _LEGACY_TABLES:
                        if table not in have:
                            continue
                        cols = [r[1] for r in src.execute("PRAGMA table_info(%s)" % table)]
                        rows = src.execute("SELECT %s FROM %s" % (",".join(cols), table)).fetchall()
                        marks = ",".join("?" * len(cols))
                        for r in rows:
                            c.execute("INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), marks), r)
                        copied = copied or bool(rows)
                    if "companion_meta" in have:
                        for k, v in src.execute("SELECT key,value FROM companion_meta WHERE key='sticker_layout'"):
                            c.execute("INSERT OR REPLACE INTO companion_meta(key,value) VALUES(?,?)", (k, v))
                    self._backfill_downloads(c)
            finally:
                src.close()
            self.set_meta("legacy_imported", "1")
            return copied

    # ---- generic meta -------------------------------------------------
    def get_meta(self, key, default=None):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM companion_meta WHERE key=?", (str(key),)).fetchone()
            return row[0] if row else default

    def set_meta(self, key, value):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO companion_meta(key,value) VALUES(?,?)", (str(key), str(value)))

    # ---- state / events -----------------------------------------------
    def load_state(self):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT data FROM companion_state WHERE id=1").fetchone()
            try:
                return json.loads(row[0]) if row else {}
            except ValueError:
                return {}

    def save_state(self, data):
        blob = json.dumps(data, separators=(",", ":"))
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO companion_state(id,data,updated_ts) VALUES(1,?,?)", (blob, time.time()))

    def add_event(self, event, payload):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO companion_events(event,payload,created_ts) VALUES(?,?,?)",
                      (event, json.dumps(payload), time.time()))
            self._event_inserts += 1
            if self._event_inserts % _PRUNE_EVERY == 0:
                c.execute("DELETE FROM companion_events WHERE seq <= (SELECT MAX(seq) FROM companion_events) - ?", (EVENT_KEEP,))

    def recent_events(self, limit=50):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT event,payload,created_ts FROM companion_events ORDER BY seq DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for ev, payload, ts in rows:
            try:
                out.append({"event": ev, "payload": json.loads(payload), "ts": ts})
            except ValueError:
                continue
        return out

    # ---- download dedupe -----------------------------------------------
    def has_download_fingerprint(self, fingerprint):
        with self._lock, self._conn() as c:
            return c.execute("SELECT 1 FROM companion_downloads WHERE fingerprint=? LIMIT 1", (str(fingerprint),)).fetchone() is not None

    def record_download(self, fingerprint, job_id="", xp=0):
        """Atomically remember a rewarded download. True only the first time."""
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO companion_downloads(fingerprint,job_id,xp,ts) VALUES(?,?,?,?)",
                            (str(fingerprint), str(job_id), int(xp), time.time()))
            return cur.rowcount == 1

    # ---- stickers --------------------------------------------------------
    def list_stickers(self):
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT sticker_id,filename,manifest FROM stickers ORDER BY created_ts DESC").fetchall()
        return [{"id": r[0], "filename": r[1], "manifest": json.loads(r[2])} for r in rows]

    def add_sticker(self, sticker_id, filename, manifest):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO stickers(sticker_id,filename,manifest,created_ts) VALUES(?,?,?,?)",
                      (str(sticker_id), str(filename), json.dumps(manifest, separators=(",", ":")), time.time()))

    def delete_sticker(self, sticker_id):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT filename FROM stickers WHERE sticker_id=?", (str(sticker_id),)).fetchone()
            if row:
                c.execute("DELETE FROM stickers WHERE sticker_id=?", (str(sticker_id),))
            return row[0] if row else None

    def save_sticker_layout(self, layout):
        self.set_meta("sticker_layout", json.dumps(layout, separators=(",", ":")))

    def load_sticker_layout(self):
        raw = self.get_meta("sticker_layout")
        try:
            return json.loads(raw) if raw else []
        except ValueError:
            return []

    # ---- unlocks ---------------------------------------------------------
    def unlock_once(self, unlock_id):
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO companion_unlocks(unlock_id,unlocked_ts) VALUES(?,?)", (str(unlock_id), time.time()))
            return cur.rowcount == 1

    def is_unlocked(self, unlock_id):
        with self._lock, self._conn() as c:
            return c.execute("SELECT 1 FROM companion_unlocks WHERE unlock_id=?", (str(unlock_id),)).fetchone() is not None

    def list_unlocks(self):
        with self._lock, self._conn() as c:
            return [r[0] for r in c.execute("SELECT unlock_id FROM companion_unlocks ORDER BY unlocked_ts").fetchall()]

    # ---- species registry (data driven, cached) -------------------------
    def species_registry(self):
        return load_registry().get("species", [])

    # ---- daily game rewards ------------------------------------------------
    def daily_game_rewards(self, day):
        with self._lock, self._conn() as c:
            row = c.execute("SELECT coins FROM companion_daily_rewards WHERE day=?", (str(day),)).fetchone()
            return int(row[0]) if row else 0

    def add_daily_game_rewards(self, day, coins):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO companion_daily_rewards(day,coins) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET coins=coins+excluded.coins",
                      (str(day), max(0, int(coins))))
            c.execute("DELETE FROM companion_daily_rewards WHERE day < date(?, '-60 day')", (str(day),))

    # ---- backup / reset --------------------------------------------------------
    def export_data(self):
        return {
            "format": "video-grabber-companion", "version": SCHEMA_VERSION,
            "state": self.load_state(), "unlocks": self.list_unlocks(),
            "stickers": [{"id": s["id"], "manifest": s["manifest"]} for s in self.list_stickers()],
            "sticker_layout": self.load_sticker_layout(),
        }

    def reset(self):
        from .core import PetState
        self.save_state(PetState().to_dict())
        with self._lock, self._conn() as c:
            for table in ("companion_inventory", "companion_unlocks", "stickers", "companion_events",
                          "companion_downloads", "companion_daily_rewards"):
                c.execute("DELETE FROM %s" % table)
            c.execute("DELETE FROM companion_meta WHERE key='sticker_layout'")

"""SQLite storage (v13): jobs, FTS5 search over jobs, download stats.

Replaces the jobs.json snapshot and the download_stats list that used to live
in settings.json. This module only knows about rows and SQL; the job registry
(jobs.JOBS) stays the in-memory source of truth and jobs.py decides what to
write and when.

Concurrency model
  * WAL journal: readers never block the writer and the writer never blocks
    readers.
  * Reads use one connection per thread (Flask request threads, the GUI
    thread, the writer thread each get their own).
  * Every write goes through write(), which takes one process-wide lock and
    runs a single BEGIN IMMEDIATE ... COMMIT transaction. Two threads can
    never interleave writes, and a failed batch rolls back as a unit.
"""
import json
import os
import sqlite3
import threading
import time
import weakref
from pathlib import Path

import settings
from logging_setup import log


SCHEMA_VERSION = 1

# download_stats keeps the newest STATS_KEEP samples: the same bound the old
# settings.json list had (record_stat trimmed it to 3600 entries).
STATS_KEEP = 3600

# Job-dict keys whose values are also copied into the searchable "metadata"
# column. The filename and url have their own columns. "error" is deliberately
# absent: it is never persisted, so a search hit on it would vanish after a
# restart.
METADATA_KEYS = ("playlist_title", "source_site", "category", "type",
                 "target_format", "subdir", "dest")

_LOCAL = threading.local()          # .conn / .path / .gen: this thread's connection
# Every open connection, for close_all(). Weak on purpose: Flask serves each
# request on a new thread, and a strong list would keep every finished
# thread's connection (and its file handles) alive for the life of the app.
_ALL_CONNS = weakref.WeakSet()
_ALL_CONNS_LOCK = threading.Lock()
_GEN = 0                            # bumped by close_all(); stale connections are dropped
_WRITE_LOCK = threading.RLock()     # one writer at a time, process-wide
_INIT_LOCK = threading.Lock()
_READY = {}                         # resolved db path -> {"fts": bool}


# ------------------------------------------------------------ connection ---
def db_path():
    return Path(settings.DB_PATH)


def _key(path):
    return str(Path(path))


class _Conn(sqlite3.Connection):
    """Plain subclass: sqlite3.Connection itself cannot be weakly referenced."""


def _connect(path):
    # isolation_level=None: autocommit, so transactions are the explicit
    # BEGIN IMMEDIATE / COMMIT pairs in write() and nothing else. Reads never
    # leave a transaction open. check_same_thread=False only so close_all()
    # may close another thread's connection at shutdown.
    c = sqlite3.connect(_key(path), timeout=30, isolation_level=None,
                        check_same_thread=False, factory=_Conn)
    mode = c.execute("PRAGMA journal_mode=WAL").fetchone()
    if not mode or str(mode[0]).lower() != "wal":
        log(f"database: WAL mode not available here (journal_mode={mode[0] if mode else '?'})")
    c.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; fsync at checkpoints
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA temp_store=MEMORY")
    return c


def _create_schema(c):
    """Create tables, indexes and (when the build has FTS5) the search index.
    Returns True when FTS5 is usable."""
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT)""")
        # seq is an explicit INTEGER PRIMARY KEY (a rowid alias) on purpose:
        # the FTS index points at it, and VACUUM may renumber the hidden rowid
        # of a table that has no such key, which would silently detach every
        # search hit from its row.
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            seq          INTEGER PRIMARY KEY,
            id           TEXT NOT NULL UNIQUE,
            url          TEXT NOT NULL DEFAULT '',
            filename     TEXT NOT NULL DEFAULT '',
            category     TEXT,
            type         TEXT,
            status       TEXT,
            phase        TEXT,
            source_site  TEXT,
            playlist_id  TEXT,
            created_ts   REAL,
            completed_ts REAL,
            failed_ts    REAL,
            size_total   INTEGER NOT NULL DEFAULT 0,
            size_done    INTEGER NOT NULL DEFAULT 0,
            archived     INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            metadata     TEXT NOT NULL DEFAULT '',
            data         TEXT NOT NULL,
            updated_ts   REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_archived_created "
                  "ON jobs (archived, created_ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)")
        c.execute("""CREATE TABLE IF NOT EXISTS download_stats (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    REAL NOT NULL,
            bytes INTEGER NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_download_stats_ts ON download_stats (ts)")
        c.execute("COMMIT")
    except BaseException:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    fts = _create_fts(c)
    c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return fts


def _create_fts(c):
    """jobs_fts: external-content FTS5 table over jobs(filename, url, metadata),
    kept in step by triggers. Returns False when this SQLite has no FTS5."""
    try:
        existed = c.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_fts'").fetchone()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
                filename, url, metadata,
                content='jobs', content_rowid='seq',
                tokenize='unicode61 remove_diacritics 2',
                prefix='2 3')""")
            c.execute("""CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
                INSERT INTO jobs_fts(rowid, filename, url, metadata)
                VALUES (new.seq, new.filename, new.url, new.metadata);
            END""")
            c.execute("""CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
                INSERT INTO jobs_fts(jobs_fts, rowid, filename, url, metadata)
                VALUES ('delete', old.seq, old.filename, old.url, old.metadata);
            END""")
            # Only when an indexed column really changed: every progress save
            # rewrites the row, and re-indexing it each time would be wasted work.
            c.execute("""CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs
                WHEN old.filename IS NOT new.filename OR old.url IS NOT new.url
                     OR old.metadata IS NOT new.metadata BEGIN
                INSERT INTO jobs_fts(jobs_fts, rowid, filename, url, metadata)
                VALUES ('delete', old.seq, old.filename, old.url, old.metadata);
                INSERT INTO jobs_fts(rowid, filename, url, metadata)
                VALUES (new.seq, new.filename, new.url, new.metadata);
            END""")
            if not existed:
                # Index created on a database that already has rows.
                c.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('rebuild')")
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return True
    except sqlite3.OperationalError as e:
        # Only "this SQLite has no FTS5" is a reason to fall back. A locked
        # or unwritable database must surface, not silently disable search.
        if "no such module" not in str(e).lower():
            raise
        log(f"database: FTS5 unavailable ({e}); search falls back to LIKE")
        return False


def _quarantine(path, why):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = Path(path)
    for suffix in ("", "-wal", "-shm"):
        src = base.with_name(base.name + suffix)
        if not src.exists():
            continue
        dst = base.with_name(f"{base.name}.corrupt-{stamp}{suffix}")
        n = 1
        while dst.exists():
            n += 1
            dst = base.with_name(f"{base.name}.corrupt-{stamp}-{n}{suffix}")
        try:
            os.replace(src, dst)
            log(f"database: {why}; moved {src.name} to {dst.name}")
        except OSError as e:
            log(f"database: couldn't move {src.name} aside ({e})")


def _ensure_ready(path):
    """Create the database and schema once per process per path."""
    k = _key(path)
    if k in _READY:
        return _READY[k]
    with _INIT_LOCK:
        if k in _READY:
            return _READY[k]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            c = None
            try:
                c = _connect(path)
                fts = _create_schema(c)
                c.close()
                break
            except sqlite3.DatabaseError as e:
                msg = str(e).lower()
                if c is not None:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
                # Corruption only. "locked" / "busy" / disk-full are not a
                # reason to move a healthy database out of the way.
                if attempt == 1 and ("malformed" in msg or "not a database" in msg):
                    _quarantine(path, f"database is unreadable ({e})")
                    continue
                raise
        _READY[k] = {"fts": fts}
        return _READY[k]


def conn():
    """This thread's connection to the current database file (opened, and the
    schema created, on first use or after settings.set_paths() moved HOME)."""
    path = db_path()
    k = _key(path)
    c = getattr(_LOCAL, "conn", None)
    if (c is not None and getattr(_LOCAL, "path", None) == k
            and getattr(_LOCAL, "gen", None) == _GEN):
        return c
    if c is not None:
        _drop(c)
        _LOCAL.conn = None
    _ensure_ready(path)
    c = _connect(path)
    with _ALL_CONNS_LOCK:
        _ALL_CONNS.add(c)
    _LOCAL.conn, _LOCAL.path, _LOCAL.gen = c, k, _GEN
    return c


def _drop(c):
    with _ALL_CONNS_LOCK:
        _ALL_CONNS.discard(c)
    try:
        c.close()
    except sqlite3.Error:
        pass


def close_all(checkpoint=True):
    """Close every connection (shutdown, tests). Forgets that any path was
    initialised, so the next conn() re-checks the schema."""
    global _GEN
    with _WRITE_LOCK:
        if checkpoint:
            try:
                conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                log(f"database: checkpoint on close failed: {e}")
        with _ALL_CONNS_LOCK:
            conns = list(_ALL_CONNS)
            _ALL_CONNS.clear()
            # Other threads still hold their (now closed) connection in
            # thread-local storage; the new generation makes conn() replace it.
            _GEN += 1
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        _LOCAL.conn = None
        _LOCAL.path = None
        _READY.clear()


def fts_available():
    conn()
    return bool(_READY.get(_key(db_path()), {}).get("fts"))


def write(fn):
    """Run fn(conn) inside one BEGIN IMMEDIATE transaction under the global
    write lock. Commits when fn returns, rolls back when it raises."""
    with _WRITE_LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            result = fn(c)
            c.execute("COMMIT")
            return result
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


# ------------------------------------------------------------------ meta ---
def meta_get(key, default=None):
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(c, key, value):
    c.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def meta_set(key, value):
    write(lambda c: _meta_set(c, key, value))


# ------------------------------------------------------------------ jobs ---
def dumps_job(snap):
    """The ONE serializer for a persisted job dict. jobs.py fingerprints this
    exact text to detect changes, so everything that writes rows uses it."""
    # ensure_ascii: a lone surrogate (it happens in Windows file names) is
    # written as a \\uXXXX escape and reads back unchanged; raw it could not
    # be bound to SQLite at all.
    return json.dumps(snap, ensure_ascii=True, default=str)


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


_INT64 = 2 ** 63 - 1


def _i(v):
    try:
        n = int(v or 0)
    except (TypeError, ValueError, OverflowError):
        try:
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            return 0
    # SQLite integers are 64-bit; a larger value must not make the row unsaveable.
    return max(-_INT64, min(_INT64, n))


def _txt(v):
    """Text safe to bind: lone surrogates (invalid in UTF-8) become '?'."""
    return v.encode("utf-8", "replace").decode("utf-8")


def _s(v):
    return None if v is None else _txt(str(v))


def metadata_text(snap):
    parts = []
    for k in METADATA_KEYS:
        v = snap.get(k)
        if v not in (None, "", False):
            parts.append(_txt(str(v)))
    return " ".join(parts)


def _job_params(jid, snap, blob, now):
    return (str(jid), _s(snap.get("url")) or "", _s(snap.get("filename")) or "",
            _s(snap.get("category")), _s(snap.get("type")), _s(snap.get("status")),
            _s(snap.get("phase")), _s(snap.get("source_site")), _s(snap.get("playlist_id")),
            _f(snap.get("created_ts")), _f(snap.get("completed_ts")), _f(snap.get("failed_ts")),
            _i(snap.get("size_total")), _i(snap.get("size_done")),
            1 if snap.get("archived") else 0,
            metadata_text(snap), blob, now)


_UPSERT_SQL = """INSERT INTO jobs (id, url, filename, category, type, status, phase,
        source_site, playlist_id, created_ts, completed_ts, failed_ts,
        size_total, size_done, archived, metadata, data, updated_ts)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        url=excluded.url, filename=excluded.filename, category=excluded.category,
        type=excluded.type, status=excluded.status, phase=excluded.phase,
        source_site=excluded.source_site, playlist_id=excluded.playlist_id,
        created_ts=excluded.created_ts, completed_ts=excluded.completed_ts,
        failed_ts=excluded.failed_ts, size_total=excluded.size_total,
        size_done=excluded.size_done, archived=excluded.archived,
        metadata=excluded.metadata, data=excluded.data, updated_ts=excluded.updated_ts"""


def apply_changes(upserts, deletes):
    """One transaction: upsert (jid, snap, blob) rows and delete ids.
    blob is dumps_job(snap), computed once by the caller.

    One row that SQLite refuses must not stop every other row from being
    saved (that would silently freeze the whole history), so if the batch
    fails it is redone row by row and only the offenders are skipped.
    Returns the ids that could not be saved."""
    if not upserts and not deletes:
        return []
    failed = []

    def run(c):
        now = time.time()
        if upserts:
            c.execute("SAVEPOINT batch")
            try:
                c.executemany(_UPSERT_SQL, [_job_params(j, s, b, now) for j, s, b in upserts])
                c.execute("RELEASE batch")
            except Exception as batch_err:
                c.execute("ROLLBACK TO batch")
                c.execute("RELEASE batch")
                log(f"database: batch save failed ({batch_err}); saving row by row")
                for j, s, b in upserts:
                    c.execute("SAVEPOINT one")
                    try:
                        c.execute(_UPSERT_SQL, _job_params(j, s, b, now))
                        c.execute("RELEASE one")
                    except Exception as e:
                        c.execute("ROLLBACK TO one")
                        c.execute("RELEASE one")
                        failed.append(j)
                        log(f"database: couldn't save job {j}: {e}")
        if deletes:
            c.executemany("DELETE FROM jobs WHERE id=?", [(str(j),) for j in deletes])
    write(run)
    return failed


def load_jobs():
    """Every stored job as (id, data_json_text), oldest first."""
    return [(r[0], r[1]) for r in conn().execute("SELECT id, data FROM jobs ORDER BY seq")]


def count_jobs():
    return conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def archived_ids():
    return {r[0] for r in conn().execute("SELECT id FROM jobs WHERE archived=1")}


def build_fts_query(q):
    """User text -> FTS5 MATCH expression. Every whitespace-separated term is
    quoted (so FTS operators and punctuation in the text are literal) and
    matches as a prefix; all terms must match. '' when nothing is searchable."""
    terms = []
    for raw in str(q or "").split():
        t = raw.replace('"', " ").strip()
        if not any(ch.isalnum() for ch in t):
            continue
        terms.append('"' + t + '"*')
    return " ".join(terms)


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_job_ids(q):
    """Ids of jobs whose filename, url or metadata match q. Uses the FTS5
    index; on a build without FTS5 it falls back to a LIKE scan. Newest
    first. [] for an empty / unsearchable query."""
    terms = [t for t in str(q or "").split() if any(ch.isalnum() for ch in t)]
    if not terms:
        return []
    c = conn()
    if fts_available():
        expr = build_fts_query(q)
        try:
            rows = c.execute(
                "SELECT j.id FROM jobs_fts JOIN jobs j ON j.seq = jobs_fts.rowid "
                "WHERE jobs_fts MATCH ? ORDER BY j.created_ts DESC, j.seq DESC", (expr,)).fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError as e:
            log(f"database: FTS query failed ({e}); using LIKE")
    where = " AND ".join(
        "(filename LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' OR metadata LIKE ? ESCAPE '\\')"
        for _ in terms)
    args = []
    for t in terms:
        pat = "%" + _like_escape(t) + "%"
        args += [pat, pat, pat]
    rows = c.execute(f"SELECT id FROM jobs WHERE {where} ORDER BY created_ts DESC, seq DESC", args)
    return [r[0] for r in rows.fetchall()]


# ----------------------------------------------------------------- stats ---
def record_stat(ts, nbytes):
    def run(c):
        c.execute("INSERT INTO download_stats(ts, bytes) VALUES (?, ?)", (float(ts), _i(nbytes)))
        c.execute("DELETE FROM download_stats WHERE id <= "
                  "(SELECT MAX(id) FROM download_stats) - ?", (STATS_KEEP,))
    write(run)


def recent_stats(limit=600):
    """The newest `limit` samples, oldest first, as {"ts", "bytes"} dicts
    (the shape /stats has always returned)."""
    rows = conn().execute(
        "SELECT ts, bytes FROM (SELECT id, ts, bytes FROM download_stats "
        "ORDER BY id DESC LIMIT ?) ORDER BY id", (int(limit),)).fetchall()
    return [{"ts": r[0], "bytes": r[1]} for r in rows]


def count_stats():
    return conn().execute("SELECT COUNT(*) FROM download_stats").fetchone()[0]


# ------------------------------------------------------------- migration ---
def _unique_sibling(path, suffix):
    dst = path.with_name(path.name + suffix)
    n = 1
    while dst.exists():
        n += 1
        dst = path.with_name(f"{path.name}{suffix}-{n}")
    return dst


def migrate_jobs_json(json_path):
    """First launch after the upgrade: import jobs.json into the jobs table
    and rename the file to jobs.json.migrated.

    Safe to run on every launch:
      * rows are inserted in ONE transaction together with a meta marker, so
        a crash leaves either nothing or everything;
      * the file is only renamed after that commit;
      * if the marker is already set and a jobs.json is back (a crash between
        commit and rename, or an old copy restored), it is just moved aside
        again and never re-imported over newer history;
      * an unreadable file is quarantined, never overwritten or deleted.
    Returns the number of jobs imported (0 when there was nothing to do)."""
    json_path = Path(json_path)
    if not json_path.exists():
        return 0
    if meta_get("jobs_json_migrated"):
        dst = _unique_sibling(json_path, ".migrated")
        try:
            os.replace(json_path, dst)
            log(f"database: jobs.json was already imported; moved it to {dst.name}")
        except OSError as e:
            log(f"database: couldn't rename jobs.json ({e})")
        return 0
    try:
        snap = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            raise ValueError("jobs.json is not a JSON object")
    except Exception as e:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = _unique_sibling(json_path, f".corrupt-{stamp}")
        try:
            os.replace(json_path, dst)
            log(f"database: couldn't read jobs.json ({e}); kept it as {dst.name}")
        except OSError as e2:
            log(f"database: couldn't read jobs.json ({e}) and couldn't move it: {e2}")
        return 0

    upserts = []
    for jid, j in snap.items():
        if not isinstance(j, dict):
            log(f"database: skipped jobs.json entry {jid!r}: not an object")
            continue
        j = dict(j)
        j.setdefault("id", str(jid))
        upserts.append((str(jid), j, dumps_job(j)))

    def run(c):
        now = time.time()
        if upserts:
            c.executemany(_UPSERT_SQL, [_job_params(i, s, b, now) for i, s, b in upserts])
        _meta_set(c, "jobs_json_migrated", time.strftime("%Y-%m-%d %H:%M:%S"))
    write(run)
    if count_jobs() < len(upserts):
        # Cannot happen unless the commit lied; never delete the source then.
        log("database: jobs.json import count mismatch; leaving jobs.json in place")
        return 0
    dst = _unique_sibling(json_path, ".migrated")
    try:
        os.replace(json_path, dst)
    except OSError as e:
        log(f"database: imported {len(upserts)} jobs but couldn't rename jobs.json: {e}")
    else:
        log(f"database: imported {len(upserts)} jobs from jobs.json (kept as {dst.name})")
    return len(upserts)


def migrate_stats_from_settings(state):
    """Move the old settings.json "download_stats" list into the table.
    Returns True when settings.json must be re-saved without that key."""
    if "download_stats" not in state:
        return False
    old = state.get("download_stats")
    if not meta_get("download_stats_migrated"):
        rows = []
        for s in (old if isinstance(old, list) else []):
            if isinstance(s, dict):
                ts, nb = _f(s.get("ts")), s.get("bytes")
                if ts is not None:
                    rows.append((ts, _i(nb)))
        rows = rows[-STATS_KEEP:]

        def run(c):
            if rows and c.execute("SELECT COUNT(*) FROM download_stats").fetchone()[0] == 0:
                c.executemany("INSERT INTO download_stats(ts, bytes) VALUES (?, ?)", rows)
            _meta_set(c, "download_stats_migrated", time.strftime("%Y-%m-%d %H:%M:%S"))
        write(run)
        log(f"database: moved {len(rows)} download stats out of settings.json")
    state.pop("download_stats", None)
    return True

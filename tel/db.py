"""SQLite access helpers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection:
    config.ensure_dirs()
    p = Path(path or config.DB_PATH)
    # A long analyse and a background model job can want the write lock at the
    # same time. Without a busy timeout SQLite fails immediately, which killed
    # a judging run mid-way. Wait rather than crash.
    conn = sqlite3.connect(p, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    if create:
        # Migrations run first: schema.sql indexes columns that an older
        # database has not grown yet, and CREATE INDEX on a missing column
        # fails the whole script.
        migrate(conn)
        conn.executescript(SCHEMA.read_text())
        conn.commit()
    migrate(conn)
    return conn


# Columns added after the first release. Applied idempotently on every connect
# so an existing database picks up schema changes without being rebuilt.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("bash_activity", "programs", "TEXT"),
    ("spans", "span_events", "TEXT"),
    ("raw_files", "inode", "INTEGER"),
    ("turns", "label_source", "TEXT"),
    ("turns", "is_system", "INTEGER"),
    ("file_activity", "via", "TEXT"),
    ("file_activity", "op_confidence", "TEXT"),
    ("turns", "label_confidence", "TEXT"),
    ("git_activity", "commit_type", "TEXT"),
    ("git_activity", "commit_scope", "TEXT"),
    ("sessions", "project_detection_method", "TEXT"),
    ("doc_gap", "session_id", "TEXT"),
    ("outcomes", "session_id", "TEXT"),
    ("narrative", "turns", "INTEGER"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()
    retire_work_streams(conn)


def retire_work_streams(conn: sqlite3.Connection) -> None:
    """Move off the workflow as a unit of work, once.

    Work streams grouped sessions by conventional-commit scope. The session is
    the unit now, so the grouping tables go, and `doc_gap` is rebuilt keyed by
    session. The judgements themselves are not lost by that: every one was
    appended to `data/doc_gaps.jsonl` when it was paid for, and the next
    analyse re-imports them, mapping each old work-stream id back to the
    sessions whose commits carried that scope.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # A database being created for the first time: connect() calls migrate()
    # once before schema.sql has run, so there is not yet a `meta` table to
    # read, and nothing to retire either. The migrate() after schema.sql does
    # the real work.
    if "meta" not in tables:
        return
    if get_meta(conn, "unit") == "session":
        return
    stale_gap = ("doc_gap" in tables and "work_stream_id" in
                 {r[1] for r in conn.execute("PRAGMA table_info(doc_gap)")})
    if stale_gap:
        conn.execute("DROP TABLE doc_gap")
    for t in ("work_stream_cost", "work_streams"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    # Descriptions written for work streams describe a thing that no longer
    # exists. The sidecar keeps them; the table should not.
    conn.execute("DELETE FROM narrative WHERE kind <> 'session'")
    set_meta(conn, "unit", "session")
    conn.commit()


# Every table keyed by session. Purging a session means clearing all of them,
# and `turns` being missing from this list is how synthetic turns kept skewing
# the friction totals after their sessions were gone.
SESSION_TABLES = [
    "api_calls", "tool_calls", "skill_calls", "file_activity", "bash_activity",
    "subagent_activity", "prompts", "responses", "errors", "metric_points",
    "spans", "events", "turns", "file_rework", "correction_cause",
    "skill_audit", "session_diagnosis", "session_dx_clean", "session_chat",
    "doc_gap", "local_session_git_context", "sessions",
]


def purge_sessions(conn: sqlite3.Connection, session_ids: list[str],
                   reason: str) -> None:
    """Delete sessions and everything keyed to them, permanently.

    The id is remembered because the collector batches: events for a session
    can land after it was deleted, and the next analyse would otherwise
    rebuild it from them.
    """
    if not session_ids:
        return
    marks = ",".join("?" for _ in session_ids)
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for table in SESSION_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})",
                     session_ids)
    for sid in session_ids:
        conn.execute("INSERT OR REPLACE INTO purged_sessions"
                     "(session_id, reason, purged_at) VALUES (?,?,?)",
                     (sid, reason, _utc_now()))
    conn.commit()


def _utc_now() -> str:
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value) if not isinstance(value, str) else value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def insert_ignore(conn: sqlite3.Connection, table: str, row: dict) -> int | None:
    """INSERT OR IGNORE; returns the rowid when a row was actually inserted."""
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})",
        tuple(row.values()),
    )
    return cur.lastrowid if cur.rowcount else None


def upsert_merge(conn: sqlite3.Connection, table: str, key_col: str, row: dict) -> None:
    """Insert a row, or fill in NULL columns of an existing row with the same key.

    Used to merge the several telemetry signals that describe one tool call:
    the decision event, the result event and (when tracing is on) the span.
    COALESCE keeps whichever signal arrived first and only fills gaps.
    """
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(
        f"{c}=COALESCE({table}.{c}, excluded.{c})" for c in row if c != key_col
    )
    sql = f"INSERT INTO {table} ({cols}) VALUES ({marks})"
    if updates:
        sql += f" ON CONFLICT({key_col}) DO UPDATE SET {updates}"
    else:
        sql += f" ON CONFLICT({key_col}) DO NOTHING"
    conn.execute(sql, tuple(row.values()))


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default=0):
    row = conn.execute(sql, params).fetchone()
    if not row or row[0] is None:
        return default
    return row[0]

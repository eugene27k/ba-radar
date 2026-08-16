"""SQLite connection and migration runner.

The database file is committed to the repository, so two things matter more than usual:
keeping it small (no excerpts, aggressive pruning) and keeping writes deterministic so
diffs stay reviewable. WAL is deliberately *not* enabled — it produces sidecar files
that would either need committing or gitignoring, and there is exactly one writer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply any migrations not yet recorded. Returns the versions applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL"
        ")"
    )
    applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")}

    newly_applied: list[int] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue

        # Transaction control has to live *inside* the script: sqlite3.executescript
        # issues an implicit COMMIT before it runs, so wrapping it in an outer
        # BEGIN/COMMIT fails with "cannot commit - no transaction is active" and
        # leaves the schema half-applied. Recording the version in the same script
        # keeps DDL and bookkeeping atomic.
        # `version` is an int parsed from the filename, so interpolating it is safe.
        conn.executescript(
            "BEGIN;\n"
            f"{path.read_text(encoding='utf-8')}\n"
            "INSERT INTO schema_migrations (version, applied_at) VALUES "
            f"({version}, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));\n"
            "COMMIT;"
        )
        newly_applied.append(version)
    return newly_applied


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def vacuum(conn: sqlite3.Connection) -> None:
    """Reclaim space after pruning. Keeps the committed file from ratcheting upward."""
    conn.execute("VACUUM")

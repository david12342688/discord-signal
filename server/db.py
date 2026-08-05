"""SQLite access shared by all pipeline stages."""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the schema applied and sane pragmas set."""
    path = Path(db_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn

import uuid
from datetime import datetime
from pathlib import Path

import duckdb

from analyst.config import SESSIONS_DIR
from analyst.ingest.loaders import LoadedTable

META = "_meta"


def _current_file() -> Path:
    return SESSIONS_DIR / ".current"


def new_session() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def session_path(session_id: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / f"{session_id}.duckdb"


def set_current_session(session_id: str) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _current_file().write_text(session_id)


def get_current_session() -> str:
    f = _current_file()
    if not f.exists():
        raise RuntimeError("No active session. Run: python -m analyst.cli ingest <file>")
    sid = f.read_text().strip()
    if not session_path(sid).exists():
        raise RuntimeError(f"Session '{sid}' database is missing. Ingest a file again.")
    return sid


def connect(session_id: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(session_path(session_id)))
    con.execute(
        f"""CREATE TABLE IF NOT EXISTS {META} (
                table_name VARCHAR,
                source VARCHAR,
                ingested_at TIMESTAMP
            )"""
    )
    return con


def _existing_tables(con) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {r[0] for r in rows}


def write_tables(con, tables: list[LoadedTable]) -> list[str]:
    """Write tables into the DB. Returns the final table names (collisions get _2, _3...)."""
    existing = _existing_tables(con)
    final_names = []

    for t in tables:
        name = t.name
        n = 1
        while name in existing:
            n += 1
            name = f"{t.name}_{n}"
        existing.add(name)

        con.register("tmp_df", t.df)
        try:
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM tmp_df')
        finally:
            con.unregister("tmp_df")

        con.execute(
            f"INSERT INTO {META} VALUES (?, ?, ?)",
            [name, t.source, datetime.now()],
        )
        final_names.append(name)

    return final_names


def list_tables(con) -> list[dict]:
    """User-facing table info. The _meta table is never included."""
    rows = con.execute(f"SELECT table_name, source FROM {META} ORDER BY ingested_at, table_name").fetchall()
    result = []
    for name, source in rows:
        n_rows = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        n_cols = con.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ?",
            [name],
        ).fetchone()[0]
        result.append({"name": name, "n_rows": n_rows, "n_cols": n_cols, "source": source})
    return result
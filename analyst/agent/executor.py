import re
import threading
from dataclasses import dataclass

import duckdb
import pandas as pd

from analyst.store.duckdb_store import session_path

MAX_ROWS = 1000
TIMEOUT_S = 30

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"install|load|pragma|call|set|truncate|vacuum|checkpoint|_meta)\b",
    re.I,
)
FILE_FUNCS = re.compile(r"\b(read_\w+|glob|\w+_scan|duckdb_\w+|pragma_\w+)\s*\(", re.I)


class UnsafeSQL(ValueError):
    pass


class QueryError(Exception):
    pass


@dataclass
class QueryResult:
    sql: str
    df: pd.DataFrame
    truncated: bool


def _strip_literals(sql: str) -> str:
    """Remove comments, string literals and quoted identifiers so keyword checks don't misfire."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def check_sql(sql: str) -> str:
    """Return the cleaned SQL, or raise UnsafeSQL with a message the model can act on."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQL("The reply contained no SQL.")
    scan = _strip_literals(sql)
    if ";" in scan:
        raise UnsafeSQL("Only one statement is allowed.")
    if not re.match(r"\s*(select|with)\b", scan, re.I):
        raise UnsafeSQL("The query must start with SELECT or WITH.")
    m = FORBIDDEN.search(scan)
    if m:
        raise UnsafeSQL(f"Forbidden keyword '{m.group(0)}'. Only read-only SELECT queries are allowed.")
    m = FILE_FUNCS.search(scan)
    if m:
        raise UnsafeSQL("File and system functions are not allowed. Query only the listed tables.")
    return sql


def run_query(session_id: str, sql: str) -> QueryResult:
    """Execute checked SQL against the session DB, read-only, with a row cap and timeout."""
    con = duckdb.connect(str(session_path(session_id)), read_only=True)
    try:
        try:
            con.execute("SET enable_external_access=false")
        except duckdb.Error:
            pass
        timer = threading.Timer(TIMEOUT_S, con.interrupt)
        timer.start()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except (duckdb.Error, duckdb.InterruptException) as e:
            msg = str(e)
            if "INTERRUPT" in msg.upper():
                msg = f"Query timed out after {TIMEOUT_S}s. Make it cheaper (filter or aggregate earlier)."
            raise QueryError(msg)
        finally:
            timer.cancel()
    finally:
        con.close()

    truncated = len(rows) > MAX_ROWS
    df = pd.DataFrame(rows[:MAX_ROWS], columns=cols)
    return QueryResult(sql=sql, df=df, truncated=truncated)
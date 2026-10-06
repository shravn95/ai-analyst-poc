from analyst.ingest.naming import clean_identifier  # noqa: F401  (kept for symmetry)

THRESHOLD = 0.98

NULL_TOKENS = ("", "na", "n/a", "null", "none", "nan", "-", "--", "?")

# Day-first formats come before month-first; the generic cast goes last (handles times, ISO 'T').
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _varchar_columns(con, table: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? AND data_type = 'VARCHAR' "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [r[0] for r in rows]


def _nullify_placeholders(con, table: str, col: str) -> None:
    tokens = ", ".join("'" + t + "'" for t in NULL_TOKENS)
    con.execute(
        f"UPDATE {_q(table)} SET {_q(col)} = NULL "
        f"WHERE LOWER(TRIM({_q(col)})) IN ({tokens})"
    )


def _numeric_expr(q: str) -> str:
    """Trim, and remove thousands separators only when they look like '1,234,567.89'."""
    return (
        f"CASE WHEN regexp_matches(TRIM({q}), '^-?[0-9]{{1,3}}(,[0-9]{{3}})+(\\.[0-9]+)?$') "
        f"THEN REPLACE(TRIM({q}), ',', '') ELSE TRIM({q}) END"
    )


def _ok_count(con, table: str, expr: str) -> int:
    return con.execute(f"SELECT COUNT({expr}) FROM {_q(table)}").fetchone()[0]


def _choose_conversion(con, table: str, col: str):
    """Return (duckdb_type, using_expr) or None if the column should stay text."""
    q, tq = _q(col), _q(table)
    n = con.execute(f"SELECT COUNT({q}) FROM {tq}").fetchone()[0]
    if n == 0:
        return None

    # 1) numbers (skip columns with leading zeros: zip codes, phone numbers, codes)
    leading_zero = con.execute(
        f"SELECT COUNT(*) FROM {tq} WHERE regexp_matches({q}, '^0[0-9]')"
    ).fetchone()[0]
    if leading_zero == 0:
        num = _numeric_expr(q)
        for typ in ("BIGINT", "DOUBLE"):
            expr = f"TRY_CAST({num} AS {typ})"
            if _ok_count(con, table, expr) / n >= THRESHOLD:
                return typ, expr

    # 2) dates / timestamps: pick the format that parses the most values
    candidates = [f"TRY_STRPTIME(TRIM({q}), '{fmt}')" for fmt in DATE_FORMATS]
    candidates.append(f"TRY_CAST(TRIM({q}) AS TIMESTAMP)")
    best_expr, best_rate = None, 0.0
    for expr in candidates:
        rate = _ok_count(con, table, expr) / n
        if rate > best_rate:          # strict '>' keeps the earlier format on ties
            best_expr, best_rate = expr, rate
    if best_expr and best_rate >= THRESHOLD:
        has_time = con.execute(
            f"SELECT COUNT(*) FROM {tq} WHERE ({best_expr}) IS NOT NULL "
            f"AND ({best_expr})::TIME <> TIME '00:00:00'"
        ).fetchone()[0]
        if has_time == 0:
            return "DATE", f"CAST({best_expr} AS DATE)"
        return "TIMESTAMP", best_expr

    return None


def fix_types(con, table_names) -> list[dict]:
    """Clean placeholders and convert text columns to real types.
    Returns a list of changes: {table, column, to, values_lost}."""
    changes = []
    for table in table_names:
        for col in _varchar_columns(con, table):
            _nullify_placeholders(con, table, col)
            choice = _choose_conversion(con, table, col)
            if choice is None:
                continue
            typ, expr = choice
            before = con.execute(f"SELECT COUNT({_q(col)}) FROM {_q(table)}").fetchone()[0]
            after = _ok_count(con, table, expr)
            con.execute(
                f"ALTER TABLE {_q(table)} ALTER {_q(col)} TYPE {typ} USING {expr}"
            )
            changes.append(
                {"table": table, "column": col, "to": typ, "values_lost": before - after}
            )
    return changes
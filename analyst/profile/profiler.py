import json
import re

from analyst.store.duckdb_store import list_tables

NUMERIC_PREFIXES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "DECIMAL",
)
INTEGER_PREFIXES = NUMERIC_PREFIXES[:9]
ID_NAME = re.compile(r"(^id_)|((^|_)id$)|(_(no|num|number|code)$)")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _num(v, nd: int = 4):
    if v is None:
        return None
    f = float(v)
    if f != f:                       # NaN
        return None
    return int(f) if f.is_integer() and abs(f) < 1e15 else round(f, nd)


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _kind_for_text(non_null: int, n_distinct: int, avg_len: float) -> str:
    if non_null == 0:
        return "empty"
    if n_distinct <= 50 and n_distinct <= 0.5 * non_null:
        return "categorical"
    if n_distinct == non_null and non_null >= 20 and avg_len <= 40:
        return "identifier"
    return "text"


def profile_column(con, table: str, col: str, dtype: str, n_rows: int) -> dict:
    q, tq = _q(col), _q(table)
    non_null, n_distinct = con.execute(
        f"SELECT COUNT({q}), COUNT(DISTINCT {q}) FROM {tq}"
    ).fetchone()
    null_count = n_rows - non_null
    null_pct = round(100 * null_count / n_rows, 1) if n_rows else 0.0

    info = {
        "name": col,
        "dtype": dtype,
        "null_count": null_count,
        "null_pct": null_pct,
        "n_distinct": n_distinct,
        "is_unique": non_null > 0 and n_distinct == non_null == n_rows,
    }

    upper = dtype.upper()
    if upper.startswith(NUMERIC_PREFIXES):
        if upper.startswith(INTEGER_PREFIXES) and ID_NAME.search(col):
            info["kind"] = "identifier"
        else:
            info["kind"] = "numeric"
            mn, mx, mean, med, std = con.execute(
                f"SELECT MIN({q}), MAX({q}), AVG({q}), MEDIAN({q}), STDDEV({q}) FROM {tq}"
            ).fetchone()
            info.update(min=_num(mn), max=_num(mx), mean=_num(mean), median=_num(med), std=_num(std))
    elif upper in ("DATE",) or upper.startswith("TIMESTAMP"):
        info["kind"] = "datetime"
        mn, mx = con.execute(f"SELECT MIN({q}), MAX({q}) FROM {tq}").fetchone()
        info.update(min=_jsonable(mn), max=_jsonable(mx))
    elif upper == "BOOLEAN":
        info["kind"] = "boolean"
    else:
        avg_len = 0.0
        if non_null:
            avg_len = con.execute(f"SELECT AVG(LENGTH({q})) FROM {tq}").fetchone()[0] or 0.0
        info["kind"] = _kind_for_text(non_null, n_distinct, avg_len)
        info["avg_len"] = round(float(avg_len), 1)

    if info["kind"] in ("categorical", "boolean") or (info["kind"] == "identifier" and n_distinct <= 10):
        rows = con.execute(
            f"SELECT {q}, COUNT(*) AS c FROM {tq} WHERE {q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY c DESC, 1 LIMIT 5"
        ).fetchall()
        info["top_values"] = [[_jsonable(v), c] for v, c in rows]

    if info["kind"] in ("text", "identifier"):
        rows = con.execute(
            f"SELECT DISTINCT {q} FROM {tq} WHERE {q} IS NOT NULL LIMIT 3"
        ).fetchall()
        info["samples"] = [str(r[0])[:60] for r in rows]

    flags = []
    if null_pct >= 30:
        flags.append("high_nulls")
    if non_null > 0 and n_distinct <= 1:
        flags.append("constant")
    info["flags"] = flags
    return info


def profile_table(con, table: str, source: str) -> dict:
    n_rows = con.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0]
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()

    columns = [profile_column(con, table, name, dtype, n_rows) for name, dtype in cols]

    sample_df = con.execute(f"SELECT * FROM {_q(table)} LIMIT 3").df()
    sample_rows = json.loads(sample_df.to_json(orient="records", date_format="iso"))
    for row in sample_rows:
        for k, v in row.items():
            if isinstance(v, str) and len(v) > 60:
                row[k] = v[:60] + "..."

    return {
        "name": table,
        "source": source,
        "n_rows": n_rows,
        "n_cols": len(columns),
        "columns": columns,
        "sample_rows": sample_rows,
    }


def profile_all(con) -> list[dict]:
    return [profile_table(con, t["name"], t["source"]) for t in list_tables(con)]
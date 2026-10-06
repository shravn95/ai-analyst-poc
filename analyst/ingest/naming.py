import re

# Words that break unquoted SQL. We suffix them so LLM-written SQL stays safe.
RESERVED = {
    "order", "group", "select", "from", "where", "table", "index", "user",
    "limit", "offset", "join", "by", "key", "check", "default", "end",
    "case", "when", "union", "all", "desc", "asc", "like", "in", "is",
    "null", "primary", "references", "values", "year", "month", "day",
}


def clean_identifier(name, prefix: str = "col") -> str:
    """Turn any string into a safe snake_case SQL identifier."""
    s = re.sub(r"[^0-9a-z]+", "_", str(name).lower().strip()).strip("_")
    if not s:
        s = prefix
    elif s[0].isdigit():
        s = f"{prefix}_{s}"
    if s in RESERVED:
        s = f"{s}_"
    return s


def clean_columns(columns: list[str]) -> list[str]:
    """Clean a list of column names and make them unique (name, name_2, name_3)."""
    seen: dict[str, int] = {}
    result = []
    for col in columns:
        base = clean_identifier(col, "col")
        if base in seen:
            seen[base] += 1
            new = f"{base}_{seen[base]}"
            while new in seen:           # extremely rare clash, e.g. "a_2" already exists
                seen[base] += 1
                new = f"{base}_{seen[base]}"
            seen[new] = 1
            result.append(new)
        else:
            seen[base] = 1
            result.append(base)
    return result
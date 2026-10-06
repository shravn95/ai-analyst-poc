import json
from analyst.store.duckdb_store import connect
from datetime import datetime
from itertools import combinations

from analyst.config import SESSIONS_DIR
from analyst.profile.profiler import profile_all

JOINABLE_KINDS = {"identifier", "categorical", "text"}


def _join_hints(tables: list[dict]) -> list[dict]:
    """Columns that share a name and type across two tables are likely join keys."""
    hints = []
    for a, b in combinations(tables, 2):
        b_cols = {c["name"]: c for c in b["columns"]}
        for ca in a["columns"]:
            cb = b_cols.get(ca["name"])
            if not cb or ca["dtype"] != cb["dtype"]:
                continue
            if ca["kind"] in JOINABLE_KINDS or cb["kind"] in JOINABLE_KINDS:
                hints.append({"left": a["name"], "right": b["name"], "column": ca["name"]})
    return hints


def build_card(con, session_id: str) -> dict:
    tables = profile_all(con)
    return {
        "session_id": session_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tables": tables,
        "join_hints": _join_hints(tables),
    }


# ---------- persistence ----------

def card_path(session_id: str):
    return SESSIONS_DIR / f"{session_id}.card.json"


def save_card(session_id: str, card: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    card_path(session_id).write_text(json.dumps(card, indent=2, default=str), encoding="utf-8")


def load_card(session_id: str) -> dict | None:
    p = card_path(session_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- text rendering (human summary now, LLM prompt context later) ----------

def _describe_column(c: dict) -> str:
    parts = [f"{c['name']} ({c['dtype']}, {c['kind']})"]
    kind = c["kind"]
    if kind == "numeric":
        parts.append(
            f"min={c.get('min')} max={c.get('max')} mean={c.get('mean')} median={c.get('median')}"
        )
    elif kind == "datetime":
        parts.append(f"{c.get('min')} -> {c.get('max')}")
    elif "top_values" in c:
        parts.append("top: " + ", ".join(f"{v} ({n})" for v, n in c["top_values"]))
    elif "samples" in c:
        parts.append("e.g. " + ", ".join(repr(s) for s in c["samples"]))

    if c["n_distinct"]:
        parts.append(f"{c['n_distinct']:,} distinct" + (", unique" if c["is_unique"] else ""))
    if c["null_count"]:
        parts.append(f"{c['null_pct']}% null")
    if c["flags"]:
        parts.append("[" + ", ".join(c["flags"]) + "]")
    return "  - " + " | ".join(parts)


def render_card_text(card: dict) -> str:
    lines = []
    for t in card["tables"]:
        lines.append(f"TABLE {t['name']}  ({t['n_rows']:,} rows x {t['n_cols']} cols, source: {t['source']})")
        lines.extend(_describe_column(c) for c in t["columns"])
        if t["sample_rows"]:
            lines.append("  sample rows:")
            lines.extend(f"    {row}" for row in t["sample_rows"])
        lines.append("")
    if card["join_hints"]:
        lines.append("POSSIBLE JOINS")
        for h in card["join_hints"]:
            lines.append(f"  {h['left']}.{h['column']} = {h['right']}.{h['column']}")
    return "\n".join(lines).rstrip()

def ensure_card(session_id: str, refresh: bool = False) -> dict:
    """Load the saved card, or build and save it if missing (or if refresh=True)."""
    card = None if refresh else load_card(session_id)
    if card is None:
        con = connect(session_id)
        try:
            card = build_card(con, session_id)
        finally:
            con.close()
        save_card(session_id, card)
    return card
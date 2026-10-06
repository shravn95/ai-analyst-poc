from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def schema_brief(card: dict) -> str:
    """Compact schema: table, row count, columns with types. Used by the router."""
    lines = []
    for t in card["tables"]:
        cols = ", ".join(f"{c['name']} ({c['dtype']})" for c in t["columns"])
        lines.append(f"{t['name']} [{t['n_rows']:,} rows]: {cols}")
    if card.get("join_hints"):
        lines.append("Possible joins: " + "; ".join(
            f"{h['left']}.{h['column']} = {h['right']}.{h['column']}" for h in card["join_hints"]
        ))
    return "\n".join(lines)


def schema_index(card: dict) -> dict[str, set[str]]:
    """{table_lower: {column_lower, ...}} for validating plans."""
    return {
        t["name"].lower(): {c["name"].lower() for c in t["columns"]}
        for t in card["tables"]
    }


def history_text(history: list[dict] | None, last_n: int = 6) -> str:
    if not history:
        return "(none)"
    return "\n".join(f"{m['role']}: {m['content']}" for m in history[-last_n:])
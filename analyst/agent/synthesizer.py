"""Answer synthesiser: turns a SQL result into a natural-language answer."""

from analyst.agent.context import history_text, load_prompt, schema_brief
from analyst.agent.sqlgen import SqlOutcome
from analyst.llm import call_llm


def synthesize_answer(
    question: str,
    outcome: SqlOutcome,
    card: dict,
    assumptions: list[str] | None = None,
    history: list[dict] | None = None,
) -> str:
    """Call the synthesiser model and return a human-friendly answer string."""

    # --- build the result summary the LLM will see ----
    df = outcome.result.df
    if df.empty:
        result_block = "(The query returned zero rows.)"
    else:
        # Show first 30 rows as TSV — compact and easy for the model to read
        preview = df.head(30).to_string(index=False)
        total = f"{len(df):,}" if not outcome.result.truncated else "1,000+"
        result_block = f"Result ({total} rows):\n{preview}"

    assumptions_text = (
        "Assumptions: " + "; ".join(assumptions) if assumptions else "None"
    )

    system = (
        load_prompt("synthesizer")
        .replace("<<SCHEMA>>", schema_brief(card))
        .replace("<<HISTORY>>", history_text(history))
    )

    user_msg = (
        f"QUESTION\n{question}\n\n"
        f"SQL\n{outcome.result.sql}\n\n"
        f"RESULT\n{result_block}\n\n"
        f"ASSUMPTIONS\n{assumptions_text}"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    return (call_llm("synthesizer", messages) or "").strip()

import re
from dataclasses import dataclass, field

from analyst.agent.context import history_text, load_prompt
from analyst.agent.executor import QueryError, QueryResult, UnsafeSQL, check_sql, run_query
from analyst.agent.schemas import Plan
from analyst.llm import PipelineError, call_llm
from analyst.profile.datacard import render_card_text

MAX_ATTEMPTS = 3


@dataclass
class SqlOutcome:
    result: QueryResult
    attempts: int
    errors: list[str] = field(default_factory=list)


def extract_sql(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    return (fence.group(1) if fence else text).strip()


def answer_with_sql(
    question: str,
    plan: Plan,
    card: dict,
    session_id: str,
    history: list[dict] | None = None,
) -> SqlOutcome:
    """Plan -> SQL -> execute. On an error, send it back to the model and retry."""
    system = (
        load_prompt("sql_gen")
        .replace("<<CARD>>", render_card_text(card))
        .replace("<<HISTORY>>", history_text(history))
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"QUESTION\n{question}\n\nPLAN\n{plan.model_dump_json(indent=2)}",
        },
    ]
    errors: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = call_llm("sql_gen", messages) or ""
        try:
            sql = check_sql(extract_sql(raw))
            result = run_query(session_id, sql)
            return SqlOutcome(result=result, attempts=attempt, errors=errors)
        except (UnsafeSQL, QueryError) as e:
            errors.append(str(e))
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"That query failed.\nError: {e}\n"
                    "Fix it. Return ONLY the corrected SQL in one ```sql block.",
                },
            ]

    raise PipelineError(
        f"Could not produce working SQL after {MAX_ATTEMPTS} attempts. Last error: {errors[-1]}"
    )
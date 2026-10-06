from analyst.agent.context import history_text, load_prompt, schema_brief
from analyst.agent.llm_json import call_json
from analyst.agent.schemas import Route


def route_question(question: str, card: dict, history: list[dict] | None = None) -> Route:
    system = (
        load_prompt("router")
        .replace("<<SCHEMA>>", schema_brief(card))
        .replace("<<HISTORY>>", history_text(history))
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    def check(r: Route) -> None:
        if r.intent == "clarify" and not r.clarifying_question:
            raise ValueError("intent 'clarify' requires a clarifying_question")

    return call_json("router", messages, Route, validate=check)
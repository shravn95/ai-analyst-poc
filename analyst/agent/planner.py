from analyst.agent.context import history_text, load_prompt, schema_index
from analyst.agent.llm_json import call_json
from analyst.agent.schemas import Plan
from analyst.profile.datacard import render_card_text


def _validator(card: dict):
    schema = schema_index(card)

    def check(plan: Plan) -> None:
        if plan.needs_clarification:
            if not plan.clarifying_question:
                raise ValueError("needs_clarification is true but clarifying_question is empty")
            return
        if not plan.steps:
            raise ValueError("steps must not be empty")

        errors = []
        for s in plan.steps:
            for t in s.tables:
                if t.lower() not in schema:
                    errors.append(
                        f"step {s.id}: unknown table '{t}' (tables are: {', '.join(schema)})"
                    )
            known = set().union(*(schema.get(t.lower(), set()) for t in s.tables)) if s.tables else set()
            for c in s.columns:
                name = c.split(".")[-1].lower()       # accept 'table.column'
                if name not in known:
                    errors.append(
                        f"step {s.id}: column '{c}' does not exist in its tables "
                        f"({', '.join(s.tables)}). Put computed values in 'details' instead"
                    )
        if errors:
            raise ValueError("; ".join(errors))

    return check


def make_plan(question: str, card: dict, history: list[dict] | None = None) -> Plan:
    system = (
        load_prompt("planner")
        .replace("<<CARD>>", render_card_text(card))
        .replace("<<HISTORY>>", history_text(history))
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    return call_json("planner", messages, Plan, validate=_validator(card))
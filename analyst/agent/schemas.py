from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal["data_query", "schema_question", "clarify", "chitchat", "out_of_scope"]


class Route(BaseModel):
    intent: Intent
    reason: str = ""
    clarifying_question: Optional[str] = None


class PlanStep(BaseModel):
    id: int
    goal: str
    operation: str = "select"       # filter / aggregate / join / rank / trend / ...
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    details: str = ""


class Plan(BaseModel):
    restated_question: str = ""
    needs_clarification: bool = False
    clarifying_question: Optional[str] = None
    steps: list[PlanStep] = Field(default_factory=list)
    expected_output: str = ""
    assumptions: list[str] = Field(default_factory=list)
import json

import pytest

from analyst.agent import llm_json
from analyst.agent.context import schema_brief
from analyst.agent.llm_json import extract_json
from analyst.agent.planner import make_plan
from analyst.agent.router import route_question
from analyst.llm import PipelineError


def fake_card():
    def col(name, dtype, kind):
        return {"name": name, "dtype": dtype, "kind": kind, "null_count": 0, "null_pct": 0.0,
                "n_distinct": 3, "is_unique": False, "flags": []}
    return {
        "session_id": "s1",
        "tables": [{
            "name": "sales", "source": "sales.csv", "n_rows": 100, "n_cols": 3,
            "columns": [col("region", "VARCHAR", "categorical"),
                        col("amount", "BIGINT", "numeric"),
                        col("order_date", "DATE", "datetime")],
            "sample_rows": [],
        }],
        "join_hints": [],
    }


def fake_llm(replies):
    """Returns a call_llm replacement that serves replies in order and records calls."""
    calls = []

    def _call(task, messages, max_retries=1):
        calls.append((task, messages))
        return replies[len(calls) - 1]

    _call.calls = calls
    return _call


# ---------- extract_json ----------

def test_extract_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_fenced_and_chatty():
    text = 'Sure! Here you go:\n```json\n{"a": {"b": 2}}\n```\nHope that helps.'
    assert extract_json(text) == {"a": {"b": 2}}


def test_extract_ignores_think_block():
    text = '<think>maybe {"x": 0}</think>{"a": 1}'
    assert extract_json(text) == {"a": 1}


def test_extract_braces_inside_strings():
    assert extract_json('{"a": "curly } brace"}') == {"a": "curly } brace"}


def test_extract_no_json():
    with pytest.raises(ValueError):
        extract_json("no json here")


# ---------- router ----------

def test_router_ok(monkeypatch):
    llm = fake_llm(['{"intent": "data_query", "reason": "asks for a total"}'])
    monkeypatch.setattr(llm_json, "call_llm", llm)
    r = route_question("total sales?", fake_card())
    assert r.intent == "data_query"


def test_router_retries_on_bad_json(monkeypatch):
    llm = fake_llm(["I think this is a data query", '{"intent": "chitchat", "reason": "greeting"}'])
    monkeypatch.setattr(llm_json, "call_llm", llm)
    r = route_question("hi", fake_card())
    assert r.intent == "chitchat"
    assert len(llm.calls) == 2


def test_router_clarify_needs_question(monkeypatch):
    bad = '{"intent": "clarify", "reason": "vague"}'
    good = '{"intent": "clarify", "reason": "vague", "clarifying_question": "Best by what?"}'
    monkeypatch.setattr(llm_json, "call_llm", fake_llm([bad, good]))
    assert route_question("show the best", fake_card()).clarifying_question == "Best by what?"


# ---------- planner ----------

def plan_json(column):
    return json.dumps({
        "restated_question": "Total amount per region",
        "steps": [{"id": 1, "goal": "sum amount by region", "operation": "aggregate",
                   "tables": ["sales"], "columns": ["region", column]}],
        "expected_output": "region, total_amount",
    })


def test_planner_valid(monkeypatch):
    monkeypatch.setattr(llm_json, "call_llm", fake_llm([plan_json("amount")]))
    plan = make_plan("sales by region", fake_card())
    assert plan.steps[0].tables == ["sales"]


def test_planner_hallucinated_column_is_retried(monkeypatch):
    llm = fake_llm([plan_json("revenue"), plan_json("amount")])
    monkeypatch.setattr(llm_json, "call_llm", llm)
    plan = make_plan("sales by region", fake_card())
    assert plan.steps[0].columns == ["region", "amount"]
    assert len(llm.calls) == 2
    # the error message about 'revenue' was sent back to the model
    assert "revenue" in llm.calls[1][1][-1]["content"]


def test_planner_gives_up_after_three_bad_replies(monkeypatch):
    monkeypatch.setattr(llm_json, "call_llm", fake_llm([plan_json("nope")] * 3))
    with pytest.raises(PipelineError):
        make_plan("sales by region", fake_card())


def test_schema_brief_lists_columns():
    text = schema_brief(fake_card())
    assert "sales" in text and "amount (BIGINT)" in text
import pandas as pd
import pytest

from analyst.agent import executor, sqlgen
from analyst.agent.executor import QueryError, UnsafeSQL, check_sql, run_query
from analyst.agent.schemas import Plan, PlanStep
from analyst.agent.sqlgen import answer_with_sql, extract_sql
from analyst.ingest.loaders import LoadedTable
from analyst.llm import PipelineError
from analyst.profile.datacard import build_card
from analyst.store import duckdb_store as store


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    sid = store.new_session()
    con = store.connect(sid)
    df = pd.DataFrame({"region": ["N", "S", "N", "E"] * 5, "amount": range(20)})
    store.write_tables(con, [LoadedTable("sales", df, "x.csv")])
    card = build_card(con, sid)
    con.close()
    return sid, card


PLAN = Plan(
    restated_question="total per region",
    steps=[PlanStep(id=1, goal="sum", operation="aggregate", tables=["sales"], columns=["region", "amount"])],
    expected_output="region, total",
)
GOOD = "```sql\nSELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region\n```"


def fake_llm(replies):
    calls = []

    def _call(task, messages, max_retries=1):
        calls.append(messages)
        return replies[len(calls) - 1]

    _call.calls = calls
    return _call


# ---------- safety ----------

@pytest.mark.parametrize("bad", [
    "DROP TABLE sales",
    "UPDATE sales SET amount = 1",
    "SELECT 1; SELECT 2",
    "SELECT * FROM read_csv('x.csv')",
    "SELECT * FROM _meta",
    "DELETE FROM sales",
])
def test_unsafe_sql_blocked(bad):
    with pytest.raises(UnsafeSQL):
        check_sql(bad)


def test_keywords_inside_strings_are_fine():
    check_sql("SELECT * FROM sales WHERE region = 'update' -- drop table")
    check_sql("WITH a AS (SELECT 1 AS x) SELECT * FROM a;")


# ---------- execution ----------

def test_run_query(session):
    sid, _ = session
    res = run_query(sid, "SELECT SUM(amount) AS s FROM sales")
    assert res.df["s"][0] == sum(range(20))
    assert not res.truncated


def test_bad_column_raises_query_error(session):
    sid, _ = session
    with pytest.raises(QueryError):
        run_query(sid, "SELECT nope FROM sales")


def test_row_cap(session, monkeypatch):
    sid, _ = session
    monkeypatch.setattr(executor, "MAX_ROWS", 5)
    res = run_query(sid, "SELECT * FROM sales")
    assert len(res.df) == 5 and res.truncated


def test_cannot_write(session):
    sid, _ = session
    with pytest.raises(QueryError):
        run_query(sid, "CREATE TABLE x AS SELECT 1")   # bypasses check_sql on purpose


# ---------- generation loop ----------

def test_extract_sql():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("Sure:\n```\nSELECT 2\n```\nDone") == "SELECT 2"
    assert extract_sql("SELECT 3") == "SELECT 3"
    assert extract_sql("<think>x</think>```sql\nSELECT 4\n```") == "SELECT 4"


def test_happy_path(session, monkeypatch):
    sid, card = session
    monkeypatch.setattr(sqlgen, "call_llm", fake_llm([GOOD]))
    out = answer_with_sql("total per region", PLAN, card, sid)
    assert out.attempts == 1
    assert list(out.result.df["region"]) == ["E", "N", "S"]


def test_error_is_fed_back_and_retried(session, monkeypatch):
    sid, card = session
    llm = fake_llm(["```sql\nSELECT revenue FROM sales\n```", GOOD])
    monkeypatch.setattr(sqlgen, "call_llm", llm)
    out = answer_with_sql("total per region", PLAN, card, sid)
    assert out.attempts == 2 and len(out.errors) == 1
    assert "revenue" in llm.calls[1][-1]["content"]


def test_unsafe_reply_is_retried(session, monkeypatch):
    sid, card = session
    monkeypatch.setattr(sqlgen, "call_llm", fake_llm(["DROP TABLE sales", GOOD]))
    assert answer_with_sql("q", PLAN, card, sid).attempts == 2


def test_gives_up_after_three(session, monkeypatch):
    sid, card = session
    monkeypatch.setattr(sqlgen, "call_llm", fake_llm(["SELECT nope FROM sales"] * 3))
    with pytest.raises(PipelineError):
        answer_with_sql("q", PLAN, card, sid)
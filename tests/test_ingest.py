import json

import duckdb
import pandas as pd
import pytest

from analyst.ingest.loaders import load_file
from analyst.ingest.naming import clean_columns, clean_identifier
from analyst.store import duckdb_store as store


# ---------- naming ----------

def test_clean_identifier():
    assert clean_identifier("Order Date ") == "order_date"
    assert clean_identifier("2024 Sales", "t") == "t_2024_sales"
    assert clean_identifier("###", "col") == "col"


def test_clean_columns_duplicates():
    assert clean_columns(["Name", "name", "NAME "]) == ["name", "name_2", "name_3"]


def test_reserved_word_suffixed():
    assert clean_identifier("Order") == "order_"


# ---------- loaders ----------

def test_csv_latin1(tmp_path):
    p = tmp_path / "people.csv"
    p.write_bytes("Name,City\nJosé,São Paulo\nZoë,Köln\n".encode("latin-1"))
    t = load_file(p)[0]
    assert t.name == "people"
    assert list(t.df.columns) == ["name", "city"]
    assert len(t.df) == 2


def test_excel_two_sheets(tmp_path):
    p = tmp_path / "book.xlsx"
    with pd.ExcelWriter(p) as w:
        pd.DataFrame({"A": [1, 2]}).to_excel(w, sheet_name="S1", index=False)
        pd.DataFrame({"B": [3]}).to_excel(w, sheet_name="S2", index=False)
    tables = load_file(p)
    assert [t.name for t in tables] == ["book_s1", "book_s2"]


def test_json_nested_flatten(tmp_path):
    p = tmp_path / "users.json"
    p.write_text(json.dumps([
        {"id": 1, "address": {"city": "Nagpur", "pin": "440013"}},
        {"id": 2, "address": {"city": "Pune", "pin": "411001"}},
    ]))
    df = load_file(p)[0].df
    assert "address_city" in df.columns
    assert len(df) == 2


def test_json_wrapped_and_jsonl(tmp_path):
    p1 = tmp_path / "wrapped.json"
    p1.write_text(json.dumps({"data": [{"a": 1}, {"a": 2}]}))
    assert len(load_file(p1)[0].df) == 2

    p2 = tmp_path / "lines.jsonl"
    p2.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    assert len(load_file(p2)[0].df) == 3


def test_txt_pipe_delimited(tmp_path):
    p = tmp_path / "data.txt"
    p.write_text("id|name|score\n1|a|10\n2|b|20\n")
    df = load_file(p)[0].df
    assert list(df.columns) == ["id", "name", "score"]
    assert len(df) == 2


def test_empty_rows_and_cols_dropped(tmp_path):
    p = tmp_path / "gaps.csv"
    p.write_text("a,b,c\n1,,3\n,,\n4,,6\n")
    df = load_file(p)[0].df
    assert list(df.columns) == ["a", "c"]
    assert len(df) == 2


def test_unsupported_extension(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="Unsupported"):
        load_file(p)


def test_missing_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_file(tmp_path / "nope.csv")


# ---------- store ----------

@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    return tmp_path / "sessions"


def test_persistence_and_meta_hidden(tmp_path, sessions_dir):
    p = tmp_path / "sales.csv"
    pd.DataFrame({"x": range(100), "y": range(100)}).to_csv(p, index=False)

    sid = store.new_session()
    con = store.connect(sid)
    store.write_tables(con, load_file(p))
    con.close()

    # reopen from disk
    con2 = duckdb.connect(str(store.session_path(sid)))
    assert con2.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 100
    con2.close()

    con3 = store.connect(sid)
    names = [t["name"] for t in store.list_tables(con3)]
    assert names == ["sales"]          # _meta hidden
    con3.close()


def test_name_collision_gets_suffix(tmp_path, sessions_dir):
    p = tmp_path / "sales.csv"
    pd.DataFrame({"x": [1]}).to_csv(p, index=False)

    con = store.connect(store.new_session())
    first = store.write_tables(con, load_file(p))
    second = store.write_tables(con, load_file(p))
    assert first == ["sales"] and second == ["sales_2"]
    con.close()


def test_current_session_roundtrip(sessions_dir):
    with pytest.raises(RuntimeError):
        store.get_current_session()
    sid = store.new_session()
    store.connect(sid).close()
    store.set_current_session(sid)
    assert store.get_current_session() == sid
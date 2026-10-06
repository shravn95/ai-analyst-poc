import pandas as pd
import pytest

from analyst.ingest.loaders import LoadedTable
from analyst.profile.datacard import build_card, render_card_text
from analyst.profile.typefix import fix_types
from analyst.store import duckdb_store as store


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path / "sessions")
    c = store.connect(store.new_session())
    yield c
    c.close()


def col_type(con, table, col):
    return con.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        [table, col],
    ).fetchone()[0]


def messy_df():
    return pd.DataFrame({
        "amount": ["1,200", "350", "N/A", "99"] * 5,
        "order_date": ["2024-01-05", "2024-02-10", "2024-03-15", "2024-04-20"] * 5,
        "zip": ["00123", "44001", "00456", "10001"] * 5,
        "city": ["Nagpur", "Pune", "Nagpur", "Pune"] * 5,
    })


def test_type_fix(con):
    names = store.write_tables(con, [LoadedTable("orders", messy_df(), "x.csv")])
    fix_types(con, names)
    assert col_type(con, "orders", "amount") == "BIGINT"
    assert col_type(con, "orders", "order_date") == "DATE"
    assert col_type(con, "orders", "zip") == "VARCHAR"      # leading zeros preserved
    assert col_type(con, "orders", "city") == "VARCHAR"
    assert con.execute("SELECT SUM(amount) FROM orders").fetchone()[0] == 5 * (1200 + 350 + 99)


def test_day_first_dates(con):
    df = pd.DataFrame({"d": ["25/12/2024", "01/02/2024", "15/08/2024", "31/01/2024"] * 5})
    names = store.write_tables(con, [LoadedTable("t", df, "x")])
    fix_types(con, names)
    assert col_type(con, "t", "d") == "DATE"
    assert str(con.execute("SELECT MIN(d) FROM t").fetchone()[0]) == "2024-01-31"
    # proves day-first: '01/02/2024' must be 1 February, 5 times in the data
    assert con.execute("SELECT COUNT(*) FROM t WHERE d = DATE '2024-02-01'").fetchone()[0] == 5


def test_card_contents(con):
    names = store.write_tables(con, [LoadedTable("orders", messy_df(), "x.csv")])
    fix_types(con, names)
    card = build_card(con, "s1")
    t = card["tables"][0]
    cols = {c["name"]: c for c in t["columns"]}

    assert t["n_rows"] == 20
    assert cols["city"]["kind"] == "categorical"
    assert cols["amount"]["kind"] == "numeric"
    assert cols["amount"]["min"] == 99 and cols["amount"]["max"] == 1200
    assert cols["amount"]["null_count"] == 5
    assert cols["order_date"]["kind"] == "datetime"
    assert "Nagpur" in render_card_text(card)


def test_join_hints(con):
    a = pd.DataFrame({"customer_id": range(1, 31), "name": [f"c{i}" for i in range(30)]})
    b = pd.DataFrame({"customer_id": [1, 2, 3] * 10, "total": range(30)})
    store.write_tables(con, [LoadedTable("customers", a, "a"), LoadedTable("orders", b, "b")])
    card = build_card(con, "s1")
    assert {"left": "customers", "right": "orders", "column": "customer_id"} in card["join_hints"]


def test_flags(con):
    df = pd.DataFrame({"x": [1] * 10, "y": [None] * 6 + [1, 2, 3, 4]})
    store.write_tables(con, [LoadedTable("t", df, "x")])
    card = build_card(con, "s1")
    cols = {c["name"]: c for c in card["tables"][0]["columns"]}
    assert "constant" in cols["x"]["flags"]
    assert "high_nulls" in cols["y"]["flags"]
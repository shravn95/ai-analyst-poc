import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analyst.ingest.naming import clean_columns, clean_identifier

ENCODINGS = ["utf-8", "utf-8-sig", "latin-1"]   # latin-1 never fails, so it's the last resort
SUPPORTED = {".csv", ".xlsx", ".xls", ".json", ".jsonl", ".txt"}


@dataclass
class LoadedTable:
    name: str
    df: pd.DataFrame
    source: str


# ---------- helpers ----------

def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully-empty rows/cols and clean column names."""
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all").copy()
    df.columns = clean_columns([str(c) for c in df.columns])
    return df.reset_index(drop=True)


def _read_text(path: Path) -> str:
    last_err = None
    for enc in ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError(f"Could not decode '{path.name}': {last_err}")


def _stringify_nested(df: pd.DataFrame) -> pd.DataFrame:
    """Lists/dicts left inside cells become JSON strings so DuckDB can store them."""
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(
                lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
            )
    return df


def _table_name(path: Path, suffix: str | None = None) -> str:
    base = path.stem if not suffix else f"{path.stem}_{suffix}"
    return clean_identifier(base, "t")


# ---------- CSV ----------

def _load_csv(path: Path) -> list[LoadedTable]:
    last_err = None
    for enc in ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, on_bad_lines="skip")
            break
        except UnicodeDecodeError as e:
            last_err = e
        except pd.errors.EmptyDataError:
            raise ValueError(f"'{path.name}' is empty.")
        except pd.errors.ParserError as e:
            raise ValueError(f"'{path.name}' is not a valid CSV: {e}")
    else:
        raise ValueError(f"Could not decode '{path.name}': {last_err}")
    return [LoadedTable(_table_name(path), _tidy(df), path.name)]


# ---------- Excel ----------

def _load_excel(path: Path) -> list[LoadedTable]:
    try:
        sheets = pd.read_excel(path, sheet_name=None)
    except ImportError as e:
        raise ValueError(f"Missing Excel reader for '{path.name}' (for .xls run: pip install xlrd): {e}")
    except Exception as e:
        raise ValueError(f"Could not read Excel file '{path.name}': {e}")

    tables = []
    for sheet_name, df in sheets.items():
        df = _tidy(df)
        if df.empty:
            continue
        tables.append(
            LoadedTable(_table_name(path, str(sheet_name)), df, f"{path.name} / {sheet_name}")
        )
    return tables


# ---------- JSON ----------

def _parse_json(text: str, name: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # maybe JSON Lines: one object per line
        rows = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                raise ValueError(f"'{name}' is neither valid JSON nor JSON Lines (bad line {i}).")
        if not rows:
            raise ValueError(f"'{name}' is empty.")
        return rows


def _records_to_df(data) -> pd.DataFrame:
    if isinstance(data, list):
        if data and all(isinstance(r, dict) for r in data):
            return pd.json_normalize(data, sep="_")
        return pd.DataFrame({"value": data})
    return pd.json_normalize(data, sep="_")


def _load_json(path: Path) -> list[LoadedTable]:
    data = _parse_json(_read_text(path), path.name)

    # {"customers": [...], "orders": [...]}  -> one table per list-of-records key
    if isinstance(data, dict):
        list_keys = [
            k for k, v in data.items()
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)
        ]
        if list_keys:
            tables = []
            for key in list_keys:
                df = _tidy(_stringify_nested(_records_to_df(data[key])))
                if not df.empty:
                    suffix = None if len(list_keys) == 1 else key
                    tables.append(LoadedTable(_table_name(path, suffix), df, f"{path.name} / {key}"))
            if tables:
                return tables
        data = [data]   # single object -> one-row table

    df = _tidy(_stringify_nested(_records_to_df(data)))
    return [LoadedTable(_table_name(path), df, path.name)]


# ---------- TXT ----------

def _try_sep(text: str, sep: str) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(io.StringIO(text), sep=sep, engine="python", on_bad_lines="skip")
    except Exception:
        return None
    return df if df.shape[1] >= 2 and len(df) > 0 else None


def _load_txt(path: Path) -> list[LoadedTable]:
    text = _read_text(path)
    if not text.strip():
        raise ValueError(f"'{path.name}' is empty.")

    candidates = []
    try:
        sample = "\n".join(text.splitlines()[:50])
        candidates.append(csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter)
    except csv.Error:
        pass
    candidates += [d for d in ["\t", "|", ";", ","] if d not in candidates]
    candidates.append(r"\s+")   # whitespace-separated as last resort

    for sep in candidates:
        df = _try_sep(text, sep)
        if df is not None:
            return [LoadedTable(_table_name(path), _tidy(df), path.name)]

    raise ValueError(
        f"Could not detect a delimiter in '{path.name}'. "
        "Tried comma, tab, pipe, semicolon and whitespace."
    )


# ---------- public API ----------

def load_file(path: Path) -> list[LoadedTable]:
    path = Path(path)
    if not path.exists():
        raise ValueError(f"File not found: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Supported: {', '.join(sorted(SUPPORTED))}"
        )

    loader = {
        ".csv": _load_csv,
        ".xlsx": _load_excel,
        ".xls": _load_excel,
        ".json": _load_json,
        ".jsonl": _load_json,
        ".txt": _load_txt,
    }[suffix]

    tables = loader(path)
    tables = [t for t in tables if not t.df.empty and t.df.shape[1] > 0]
    if not tables:
        raise ValueError(f"No usable data found in '{path.name}'.")
    return tables
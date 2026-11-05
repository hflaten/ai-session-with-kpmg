"""CSV / Excel -> one Postgres table per file (or per Excel sheet), via pandas.

Every table gets a comment, and every column gets a comment describing it,
so the agent sees what the data means in its system prompt (describe_schema).
Column comments are a generated summary of the data, unless the caller
supplies its own text in column_descriptions.
"""
import io
import re
from datetime import date

import pandas as pd
from psycopg import sql
from sqlalchemy import create_engine

from app import db
from app.config import settings

RESERVED_TABLES = {"agent_runs", "document_chunks"}  # never overwrite the backend's own tables
MAX_IDENTIFIER = 63                                   # Postgres name length limit


def to_identifier(name: str) -> str:
    """'Revenue (NOK)' -> 'revenue_nok'. Safe as a Postgres table/column name."""
    ident = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    if not ident or ident[0].isdigit():
        ident = f"c_{ident}" if ident else "column"
    return ident[:MAX_IDENTIFIER]


def unique_identifiers(headers: list) -> list[str]:
    """snake_case every header and make duplicates unique: amount, amount_2, ..."""
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        base = to_identifier(h)
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}"[:MAX_IDENTIFIER])
    return out


def read_file(filename: str, data: bytes) -> dict[str | None, pd.DataFrame]:
    """{sheet name (None for CSV): DataFrame}"""
    lower = filename.lower()
    if lower.endswith(".csv"):
        return {None: pd.read_csv(io.BytesIO(data), sep=None, engine="python")}  # sniffs , or ;
    if lower.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(data), sheet_name=None)
    raise ValueError("Upload a .csv or .xlsx file")


def _parse_dates(series: pd.Series) -> pd.Series:
    """Turn text columns that are entirely dates into real dates; leave others alone."""
    is_text = pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)
    if not is_text or series.dropna().empty:
        return series
    try:
        return pd.to_datetime(series, format="mixed")
    except (ValueError, TypeError):
        return series


def _fmt(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if value == value.normalize() else value.isoformat()
    return str(value)


def profile_column(original_header, series: pd.Series) -> str:
    """Generated column comment, e.g.
    'Original header "Revenue (NOK)". 24 values, 0 empty. Range 49500 to 300000. Examples: 240000, 135000'
    """
    values = series.dropna()
    parts = [f'Original header "{original_header}".', f"{len(values)} values, {series.isna().sum()} empty."]
    if values.empty:
        return " ".join(parts)
    if pd.api.types.is_bool_dtype(series):
        parts.append("True/false values.")
    elif pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
        parts.append(f"Range {_fmt(values.min())} to {_fmt(values.max())}.")
    else:
        parts.append(f"{values.nunique()} distinct values.")
    examples = [_fmt(v)[:40] for v in values.value_counts().index[:3]]
    parts.append("Examples: " + ", ".join(examples))
    return " ".join(parts)


def _engine():
    # SQLAlchemy needs the driver in the URL: postgresql+psycopg://
    return create_engine(re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", settings.db_url))


def ingest_table(filename: str, data: bytes, column_descriptions: dict[str, str] | None = None) -> dict:
    descriptions = column_descriptions or {}
    used_descriptions: set[str] = set()
    sheets = {k: v for k, v in read_file(filename, data).items() if not v.empty}
    if not sheets:
        raise ValueError("The file has no data rows")

    file_stem = re.sub(r"\.[^.]+$", "", filename)
    engine = _engine()
    tables = []
    try:
        for sheet, df in sheets.items():
            name = to_identifier(file_stem if len(sheets) == 1 else f"{file_stem}_{sheet}")
            if name in RESERVED_TABLES:
                raise ValueError(f"'{name}' is reserved; rename the file")

            originals = list(df.columns)
            df.columns = unique_identifiers(originals)
            df = df.apply(_parse_dates)

            # Column comments: caller's text (matched on original header or new name), else generated
            columns = []
            for original, column in zip(originals, df.columns):
                key = str(original) if str(original) in descriptions else column
                if key in descriptions:
                    used_descriptions.add(key)
                    comment = descriptions[key]
                else:
                    comment = profile_column(original, df[column])
                columns.append({"name": column, "original_header": str(original), "comment": comment})

            df.to_sql(name, engine, schema="public", if_exists="replace", index=False)

            table_comment = (f'Uploaded from "{filename}"' + (f', sheet "{sheet}"' if sheet else "")
                             + f" on {date.today().isoformat()}. {len(df)} rows.")
            with db.connect() as conn:
                conn.execute(sql.SQL("comment on table {} is {}").format(
                    sql.Identifier("public", name), sql.Literal(table_comment)))
                for col in columns:
                    conn.execute(sql.SQL("comment on column {} is {}").format(
                        sql.Identifier("public", name, col["name"]), sql.Literal(col["comment"])))

            tables.append({"table": name, "rows": len(df), "comment": table_comment, "columns": columns})
    finally:
        engine.dispose()

    return {"tables": tables, "unused_column_descriptions": sorted(set(descriptions) - used_descriptions)}

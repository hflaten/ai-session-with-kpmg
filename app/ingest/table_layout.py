"""Turn a messy spreadsheet sheet into a clean table.

Real report-style sheets (e.g. company production tables) are not tidy CSVs:

    r0  E&P Norway                                      <- title rows
    r1  Full year 2025        Equinor share  Produced volume              <- group labels
    r2           1000 boed    %              Liquid   Gas    Total        <- sub labels
    r3           Aasta Hansteen 0.51         0.61     60.3   60.9         <- data
    ..
    r53          Total Equinor-operated      642.9    663.4  1306.3       <- totals row
    r54          * New field on stream from December'25                   <- footnote

clean_sheet() reads the raw grid (no header assumed) and:
1. drops fully empty rows and columns,
2. finds the real header row (first row in the top 15 that is mostly text labels, no data),
3. combines a two-row header (group label above sub label) into one name per column,
4. skips clearly labelled totals rows ("Total ...", "Grand total ...", "Sum ..."),
5. moves footnotes (rows with a single text cell) into notes for the table comment,
6. turns several tables in one sheet into one table with a `section` column: stacked
   (a repeated header mid-sheet) or side by side (blocks separated by an empty column
   with the same sub-headers),
7. converts mostly-numeric columns to numbers ("45.9%" -> 0.459, "-" -> empty),
8. names "%" columns by what they hold: 0-1 fractions get no "pct" (comment says
   "fraction, 0.51 = 51%"); values above 1 are percentages and get "pct".
"""
import math
import re
from dataclasses import dataclass, field

import pandas as pd

HEADER_SEARCH_ROWS = 15
TOTAL_LABEL = re.compile(r"^(grand\s+)?(sub)?totals?\b|^sum\b", re.IGNORECASE)
UNIT_ONLY = re.compile(r"^\(?\d[\d .,]*\s*[A-Za-z/%³]+\)?$|^%$")   # "1000 boed", "(NOK m)", "%"
THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?%?$")   # "1,234" or "12,345.6"
EMPTY_MARKERS = {"-", "–", "—", "n/a", "na", "n.a.", ""}


@dataclass
class CleanTable:
    df: pd.DataFrame                    # columns = display names (still un-snake_cased)
    original_headers: list[str]         # header text as written, e.g. "Produced volume / Liquid"
    notes: list[str] = field(default_factory=list)              # footnotes + header notes
    skipped_totals: list[str] = field(default_factory=list)     # labels of skipped totals rows
    non_numeric: dict[int, list[str]] = field(default_factory=dict)  # column position -> values set to empty
    fractions: set[int] = field(default_factory=set)            # positions of "%" columns stored as 0-1 fractions
    header_rows: list[int] = field(default_factory=list)        # 0-based rows used as header (in the sheet)


# ---------- cell helpers ----------

def _is_empty(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def to_number(v):
    """Number or None. Accepts 12, "12.5", "1,234", "45.9%" (-> 0.459). "0,2,4" is a list, not a number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else v
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace(" ", "")
        if "," in s:
            if not THOUSANDS.match(s):   # "0,2,4" is a list, not 24
                return None
            s = s.replace(",", "")
        pct = s.endswith("%")
        try:
            n = float(s[:-1] if pct else s)
        except ValueError:
            return None
        return n / 100 if pct else n
    return None


def _is_year(v) -> bool:
    n = to_number(v)
    return n is not None and float(n).is_integer() and 1900 <= n <= 2100


def _is_label(v) -> bool:
    """Header-ish cell: text, or a year like 2025 (wide tables have year columns)."""
    return (not _is_empty(v) and to_number(v) is None) or _is_year(v)


def _has_data_number(row: pd.Series) -> bool:
    return any(to_number(v) is not None and not _is_year(v) for v in row if not _is_empty(v))


def _text(v) -> str:
    if _is_empty(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)                                   # 2025.0 -> "2025"
    return re.sub(r"\s+", " ", str(v)).strip()


# ---------- the steps ----------

def _find_header_row(grid: pd.DataFrame) -> int:
    """First row (within the top rows) that is mostly labels and holds no data numbers."""
    data_widths = [row.notna().sum() for _, row in grid.iterrows() if _has_data_number(row)]
    width = int(pd.Series(data_widths).median()) if data_widths else int(grid.notna().sum(axis=1).max())
    needed = max(2, math.ceil(width / 2))
    for pos in range(min(HEADER_SEARCH_ROWS, len(grid))):
        row = grid.iloc[pos]
        if not _has_data_number(row) and sum(_is_label(v) for v in row) >= needed:
            return pos
    return 0  # nothing looks like a header: use the first row, like before


def _is_header_like(row: pd.Series, data_cols: list) -> bool:
    """No data numbers and at least one label in a column that holds data."""
    return not _has_data_number(row) and any(_is_label(row[c]) for c in data_cols)


def _is_group_row(row: pd.Series, data_cols: list) -> bool:
    """A second header row: header-like, and not a lone title (one cell in the first column)."""
    labels = [c for c in data_cols if _is_label(row[c])]
    return _is_header_like(row, data_cols) and (len(labels) >= 2 or labels[0] != data_cols[0])


def _combine_headers(upper: list[str] | None, lower: list[str]) -> list[tuple[str, str]]:
    """[(display name, original text)] per column. Group labels fill rightwards over empty
    upper cells (not from the first column, which labels the row names)."""
    if upper is None:
        return [(lab, lab) for lab in lower]
    filled, current = [], ""
    for i, lab in enumerate(upper):
        if lab:
            current = lab if i > 0 else ""
            filled.append(lab)
        else:
            filled.append(current)
    out = []
    for up, low in zip(filled, lower):
        parts = [p for p in (up, low) if p]
        out.append((" ".join(parts), " / ".join(parts)))
    return out


@dataclass
class _Block:
    """One table inside the sheet: its columns, data rows and header labels."""
    cols: list
    rows: list[pd.Series]
    row_sections: list[str | None]     # section label per row (None = the block's first section)
    upper: list[str] | None            # group-label row, if the header has two rows
    lower: list[str]                   # label row
    numeric_cols: list
    notes: list[str]
    totals: list[str]
    found_sections: bool               # a repeated header inside the block started a new section


def _split_blocks(cols: list) -> list[list]:
    """Columns separated by fully empty columns are separate blocks (tables side by side)."""
    blocks = [[cols[0]]]
    for c in cols[1:]:
        if c == blocks[-1][-1] + 1:
            blocks[-1].append(c)
        else:
            blocks.append([c])
    return blocks


def _read_block(grid: pd.DataFrame, header_pos: list[int], cols: list) -> _Block:
    data = grid.iloc[header_pos[-1] + 1:][cols]

    def numeric_share(col):
        values = [v for v in data[col] if not _is_empty(v)]
        return sum(to_number(v) is not None for v in values) / len(values) if values else 0
    numeric_cols = [c for c in cols if numeric_share(c) >= 0.6]

    # walk the rows: totals, footnotes, repeated headers (= new section), data
    notes, totals, rows, row_sections = [], [], [], []
    current_section, in_header_run, found_sections = None, False, False
    for _, row in data.iterrows():
        cells = [v for v in row if not _is_empty(v)]
        if not cells:
            continue
        first_text = next((_text(v) for v in cells if to_number(v) is None), "")
        if TOTAL_LABEL.match(first_text):
            totals.append(first_text)
            in_header_run = False
            continue
        if len(cells) == 1 and to_number(cells[0]) is None and len(cols) > 1:
            notes.append(_text(cells[0]))                          # footnote
            continue
        if not _has_data_number(row) and any(_is_label(row[c]) for c in numeric_cols):
            if not in_header_run:                                  # first row of a repeated header
                current_section = _text(row[cols[0]]) or None
                found_sections = True
            in_header_run = True
            continue
        in_header_run = False
        rows.append(row)
        row_sections.append(current_section)

    labels = [[_text(grid.iloc[p][c]) for c in cols] for p in header_pos]
    upper, lower = (labels[0], labels[1]) if len(labels) == 2 else (None, labels[0])
    return _Block(cols, rows, row_sections, upper, lower, numeric_cols, notes, totals, found_sections)


def _same_layout(blocks: list[_Block]) -> bool:
    """Side-by-side blocks with identical sub-labels (ignoring the first column) can be stacked."""
    def key(b):
        return [lab.lower() for lab in b.lower[1:]]
    return len(blocks) > 1 and all(len(b.cols) > 1 and key(b) == key(blocks[0]) for b in blocks)


def clean_sheet(raw: pd.DataFrame) -> CleanTable:
    grid = raw.map(lambda v: None if _is_empty(v) else (v.strip() if isinstance(v, str) else v))
    grid = grid.dropna(how="all").dropna(axis=1, how="all")
    if grid.empty:
        return CleanTable(df=pd.DataFrame(), original_headers=[])

    # 1. header row(s)
    h = _find_header_row(grid)
    below = grid.iloc[h + 1:]
    data_cols = [c for c in grid.columns if below[c].notna().any()]
    header_pos = [h]
    has_numbers = any(_has_data_number(row) for _, row in below.iterrows())
    if not has_numbers:
        pass                     # text-only table: every row looks like a header, so assume one header row
    elif h + 1 < len(grid) and _is_group_row(grid.iloc[h + 1], data_cols):
        header_pos = [h, h + 1]                                   # group row, then sub row below
    elif h > 0 and _is_group_row(grid.iloc[h - 1], data_cols):
        header_pos = [h - 1, h]                                   # group row above the label row

    # 2. one table, or several side by side with the same columns (-> stacked)
    side_by_side = [_read_block(grid, header_pos, cols) for cols in _split_blocks(data_cols)]
    blocks = side_by_side if _same_layout(side_by_side) else [_read_block(grid, header_pos, data_cols)]
    first = blocks[0]
    has_sections = len(blocks) > 1 or any(b.found_sections for b in blocks)

    notes = list(dict.fromkeys(n for b in blocks for n in b.notes))    # in order, no duplicates
    totals = [t for b in blocks for t in b.totals]
    rows, sections = [], []
    for k, b in enumerate(blocks):
        block_title = (b.upper[0] if b.upper else "") or f"section {k + 1}"
        for row, section in zip(b.rows, b.row_sections):
            rows.append(list(row))                                 # by position: blocks have different columns
            sections.append(section or block_title)

    # 3. column names (from the first block; stacked blocks share them)
    upper = list(first.upper) if first.upper else None
    lower = list(first.lower)
    first_is_text = first.cols[0] not in first.numeric_cols
    # A unit on the row-name column ("1000 boed") describes the numbers, not the names: keep it as a note
    if lower[0] and UNIT_ONLY.match(lower[0]) and first_is_text:
        notes.insert(0, f"Header unit: {lower[0]}")
        lower[0] = ""
    if has_sections and upper:                  # the first column's group label is the section title
        upper[0] = ""
    named = _combine_headers(upper, lower)
    names = [name or ("name" if i == 0 else f"column {i + 1}") for i, (name, _) in enumerate(named)]
    originals = [orig or name for (name, orig), name in zip(named, names)]

    df = pd.DataFrame(rows, columns=names) if rows else pd.DataFrame(columns=names)

    # 4. numbers: convert numeric columns, remember what didn't convert
    #    (by position: two columns can have the same display name until they are made unique)
    non_numeric: dict[int, list[str]] = {}
    for i, col in enumerate(first.cols):
        if col not in first.numeric_cols:
            continue
        values = df.iloc[:, i]
        bad = sorted({_text(v) for v in values if not _is_empty(v) and to_number(v) is None
                      and _text(v).lower() not in EMPTY_MARKERS})
        if bad:
            non_numeric[i] = bad
        df.isetitem(i, pd.to_numeric(values.map(to_number), errors="coerce"))

    # "%" columns: values between 0 and 1 are fractions (0.51 = 51%) -> no "pct" in the name.
    # Values above 1 are percentages (51 = 51%) -> "pct".
    fractions: set[int] = set()
    for i, name in enumerate(names):
        if "%" not in name:
            continue
        values = df.iloc[:, i].dropna() if first.cols[i] in first.numeric_cols else pd.Series(dtype=float)
        if len(values) and values.between(0, 1).all():
            fractions.add(i)
            names[i] = re.sub(r"\s*%\s*", " ", name).strip() or f"column {i + 1}"
        else:
            names[i] = name.replace("%", "pct")
    df.columns = names

    # 5. stacked or side-by-side tables -> section column
    if has_sections:
        df.insert(0, "section", sections, allow_duplicates=True)
        originals.insert(0, "section (title of each table in the sheet)")
        non_numeric = {i + 1: v for i, v in non_numeric.items()}   # positions shift by one
        fractions = {i + 1 for i in fractions}

    return CleanTable(df=df, original_headers=originals, notes=notes, skipped_totals=totals,
                      non_numeric=non_numeric, fractions=fractions,
                      header_rows=[int(grid.index[p]) for p in header_pos])

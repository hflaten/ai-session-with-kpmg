"""Messy spreadsheet layouts -> clean tables (generated files, no network)."""
import io

import openpyxl
import pandas as pd

from app.ingest.table_layout import clean_sheet
from app.ingest.tabular import read_file, unique_identifiers


def xlsx_bytes(rows: list[list], merges: list[str] = ()) -> bytes:
    """Build an .xlsx in memory. rows: list of rows (None = empty cell)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            if value is not None:
                ws.cell(row=r, column=c, value=value)
    for m in merges:
        ws.merge_cells(m)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def clean(rows, merges=()):
    raw = read_file("report.xlsx", xlsx_bytes(rows, merges))["Report"]
    t = clean_sheet(raw)
    t.df.columns = unique_identifiers(list(t.df.columns))
    return t


# 3 title rows, an empty row, a two-row header (merged group label), an empty column,
# data with a "-" and a text value in a numeric column, a totals row and a footnote.
REPORT = [
    ["Acme Group"],                                                        # title
    ["Regional revenue report"],                                          # title
    ["Full year 2025 (NOK million)"],                                     # title
    [],                                                                    # empty row
    [None, "Region",  None, "Revenue", None,   None,    "Margin"],        # group labels (B5, D5:F5 merged)
    [None, None,      None, "Q1",      "Q2",   "Total", "%"],             # sub labels
    [None, "Oslo",    None, 100,       120,    220,     "45.5%"],
    [None, "Bergen",  None, 80,        "-",    80,      0.41],
    [None, "Trondheim", None, 60,      70,     130,     "n/a"],
    [None, "Stavanger", None, 50,      55,     105,     "Varies"],
    [None, "Total",   None, 290,       245,    535,     None],            # totals row: skipped
    [None, "* Q2 Bergen not reported yet"],                               # footnote
]


def test_title_rows_and_two_row_header():
    t = clean(REPORT, merges=["D5:F5"])
    assert list(t.df.columns) == ["region", "revenue_q1", "revenue_q2", "revenue_total", "margin_pct"]
    assert t.original_headers == ["Region", "Revenue / Q1", "Revenue / Q2", "Revenue / Total", "Margin / %"]
    assert t.header_rows == [4, 5]                         # 0-based sheet rows 5 and 6


def test_rows_totals_footnotes_and_numbers():
    t = clean(REPORT, merges=["D5:F5"])
    assert t.df["region"].tolist() == ["Oslo", "Bergen", "Trondheim", "Stavanger"]   # no title/total/footnote rows
    assert t.skipped_totals == ["Total"]
    assert t.notes == ["* Q2 Bergen not reported yet"]
    assert t.df["revenue_q2"].isna().tolist() == [False, True, False, False]           # "-" -> empty
    assert t.df["margin_pct"].tolist()[:2] == [0.455, 0.41]                            # "45.5%" -> 0.455
    assert t.non_numeric == {4: ["Varies"]}                                            # "n/a" is just empty
    assert t.df["revenue_total"].sum() == 535                                          # = the skipped totals row


def test_works_without_merged_cells_too():
    """Some sheets don't merge the group label; it still applies to the empty cells to its right."""
    assert list(clean(REPORT).df.columns) == ["region", "revenue_q1", "revenue_q2", "revenue_total", "margin_pct"]


def test_tidy_csv_is_unchanged():
    raw = read_file("x.csv", b"Region;Revenue (NOK)\nOslo;100\nBergen;80\n")[None]
    t = clean_sheet(raw)
    assert list(t.df.columns) == ["Region", "Revenue (NOK)"] and len(t.df) == 2
    assert t.df["Revenue (NOK)"].tolist() == [100, 80]


def test_year_columns_count_as_header_labels():
    t = clean([["Revenue by year"], ["Region", 2024, 2025], ["Oslo", 10.5, 12.0], ["Bergen", 8.0, 9.5]])
    assert list(t.df.columns) == ["region", "c_2024", "c_2025"] and len(t.df) == 2


def test_stacked_tables_become_a_section_column():
    t = clean([
        ["Fields"],
        ["Operated fields", "Share", "Volume"],
        ["unit", "%", "kboed"],
        ["Alpha", 0.5, 10.0],
        ["Total operated", None, 10.0],
        [],
        ["Partner fields", "Share", "Volume"],
        ["unit", "%", "kboed"],
        ["Beta", 0.2, 4.0],
        ["Gamma", 0.3, 6.0],
    ])
    assert t.df["section"].tolist() == ["Operated fields", "Partner fields", "Partner fields"]
    assert t.df.iloc[:, 1].tolist() == ["Alpha", "Beta", "Gamma"]
    assert t.skipped_totals == ["Total operated"]


def test_side_by_side_tables_with_same_columns_are_stacked():
    t = clean([
        ["Operated", "Share", "Volume", None, "Partner", "Share", "Volume"],
        ["1000 boed", "%", "Total", None, "1000 boed", "%", "Total"],
        ["Alpha", 0.5, 10.0, None, "Beta", 0.2, 4.0],
        [None, None, None, None, "Gamma", 0.3, 6.0],
    ])
    assert list(t.df.columns) == ["section", "name", "share_pct", "volume_total"]
    assert t.df["section"].tolist() == ["Operated", "Partner", "Partner"]
    assert t.df["name"].tolist() == ["Alpha", "Beta", "Gamma"]
    assert t.notes == ["Header unit: 1000 boed"]


def test_single_column_table_keeps_its_rows():
    t = clean([["Names"], ["Oslo"], ["Bergen"]])
    assert t.df.iloc[:, 0].tolist() == ["Oslo", "Bergen"] and t.notes == []

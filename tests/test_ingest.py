"""Chunking and column profiling (pure functions, no network)."""
import pandas as pd

from app.ingest.pdf import make_chunks, split_page
from app.ingest.tabular import profile_column, read_file, to_identifier, unique_identifiers


def test_short_page_is_one_chunk():
    assert split_page("hello world") == ["hello world"]
    assert split_page("") == []


def test_long_page_splits_with_overlap_at_word_boundaries():
    text = " ".join(f"word{i}" for i in range(2000))   # ~15k chars
    chunks = split_page(text, size=3200, overlap=300)
    assert len(chunks) >= 5
    assert all(len(c) <= 3200 for c in chunks)
    assert all(not c.startswith(" ") and "word" in c.split()[0] for c in chunks)  # no split words
    assert chunks[0][-200:] in chunks[1]                                        # overlap


def test_chunks_never_cross_pages_and_ids_are_readable():
    chunks = make_chunks("r.pdf", ["page one text", "", "page three text"])
    assert [(c["page_number"], c["chunk_index"], c["chunk_id"]) for c in chunks] == [
        (1, 0, "r.pdf:p1:c0"), (3, 1, "r.pdf:p3:c1")]


def test_identifiers():
    assert to_identifier("Revenue (NOK)") == "revenue_nok"
    assert to_identifier("2026 Sales") == "c_2026_sales"
    assert unique_identifiers(["Amount", "amount", "AMOUNT!"]) == ["amount", "amount_2", "amount_3"]


def test_profile_numeric_text_and_dates():
    num = profile_column("Revenue (NOK)", pd.Series([240000, 135000, None]))
    assert num.startswith('Original header "Revenue (NOK)". 2 values, 1 empty. Range 135000 to 240000.')
    text = profile_column("Region", pd.Series(["Oslo", "Oslo", "Bergen"]))
    assert "2 distinct values. Examples: Oslo, Bergen" in text
    dates = profile_column("Month", pd.to_datetime(pd.Series(["2026-01-01", "2026-03-01"])))
    assert "Range 2026-01-01 to 2026-03-01" in dates


def test_csv_with_semicolons_is_read():
    frames = read_file("x.csv", b"a;b\n1;2\n3;4\n")
    assert list(frames[None].columns) == ["a", "b"]

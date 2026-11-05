"""Generate a small, made-up 3-page report for testing RAG.

    .venv/bin/python -m scripts.make_test_pdf

Writes tests/fixtures/nordvik_q1_2026_report.pdf. The company and all facts
are fictional. The facts are designed to combine with the sample_sales table
(e.g. the margin target here vs. actual margins in the database).
"""
from pathlib import Path

from fpdf import FPDF

OUT = Path("tests/fixtures/nordvik_q1_2026_report.pdf")

PAGES = [
    ("Nordvik Advisory - Q1 2026 Management Report", [
        "FICTIONAL DOCUMENT - generated for software testing only.",
        "Executive summary",
        "Nordvik Advisory sells two products through three regional offices: Audit Pro, a fixed-scope "
        "audit engagement, and Tax Assist, a recurring tax-filing service. The offices are Oslo, Bergen "
        "and Trondheim.",
        "Margin target. For Q1 2026 (January to March), management set a target gross margin of at "
        "least 45 percent for every regional office. Gross margin is defined as revenue minus direct "
        "delivery cost, divided by revenue. Offices below the target must present a recovery plan to "
        "the board in May 2026.",
        "Group priorities for 2026 are growing Tax Assist volumes, keeping Audit Pro pricing stable, "
        "and improving utilisation of delivery staff.",
    ]),
    ("Regional commentary", [
        "Oslo. Demand for Tax Assist was strong throughout the quarter, helped by a new partnership "
        "with two accounting software vendors. Audit Pro pricing was unchanged.",
        "Bergen. The office hired four junior auditors in February 2026 to prepare for a large "
        "client portfolio starting in Q2. Their onboarding and training time was charged as direct "
        "delivery cost while they were not yet billable, which raised Bergen's costs in Q1. "
        "Management expects Bergen's margin to recover from Q2 2026 as the new staff become billable.",
        "Trondheim. To win public-sector clients, Trondheim offered a 15 percent introductory "
        "discount on Audit Pro from January 2026. The discount lowers revenue per engagement while "
        "delivery cost stays the same, so it reduces the office's margin. The discount is planned "
        "to end in June 2026.",
    ]),
    ("Outlook and risks", [
        "The 45 percent gross margin target remains in place for Q2 2026.",
        "Main risks: slower conversion of public-sector leads in Trondheim after the discount ends; "
        "wage inflation for experienced auditors in Oslo; and the risk that the new Bergen client "
        "portfolio starts later than planned.",
        "Next review: the board will review regional margins and recovery plans on 20 May 2026.",
    ]),
]


def build() -> None:
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    for title, paragraphs in PAGES:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 9, title)
        pdf.ln(4)
        for text in paragraphs:
            bold = len(text) < 40  # short lines are sub-headings
            pdf.set_font("Helvetica", "B" if bold else "", 12 if bold else 11)
            pdf.multi_cell(0, 6, text)
            pdf.ln(3)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(PAGES)} pages)")


if __name__ == "__main__":
    build()

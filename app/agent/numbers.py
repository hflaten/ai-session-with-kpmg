"""Check that the numbers and dates written in a claim come from the cited values.

    claim_mismatches("Order 40 units by 9 Nov; lost sales fall to NOK 10k",
                     values=[40, "2026-11-09", 9745])   -> []   (all found)

Numbers may be rounded and formatted the way people write them:
    414493  ~ "414,493" / "414 493" / "414k" / "414,000" / "NOK 0.41m"   (rounded to the precision shown)
    0.0357  ~ "3.6%"            (a fraction written as a percent)
    1.724   ~ "72%" / "+72 %"   (a factor written as a change: 1.724 = 72% more)
    a - b   ~ the difference of two cited values ("saves NOK 405k" from 414,493 and 9,745)
Dates match on day and month: "2026-11-11" ~ "11 Nov" / "11. november" / "Nov 11" / "1-10 Dec" (each end).
Text that is part of a name (e.g. "Røkt laks 300 g") is removed first, and so are years.
"""
import re
from datetime import date
from itertools import combinations

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "des": 12}
MONTH = r"(jan|feb|mar|apr|ma[iy]|jun|jul|aug|sep|o[ck]t|nov|de[cs])[a-z]*\.?"
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\.?(?:\s*[–-]\s*(\d{{1,2}})\.?)?\s+{MONTH}", re.IGNORECASE)
MONTH_DAY = re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:\s*[–-]\s*(\d{{1,2}}))?\b", re.IGNORECASE)
YEAR = re.compile(r"\b20[2-3]\d\b")

# 414,493 | 414 493 | 1.3 | 0,5 | 72 ; then an optional k / m / % suffix
NUMBER = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[,  ]\d{3})+|\d+)(?:([.,])(\d+))?\s*(k|m|mill(?:ion)?|%|percent|pct)?(?![\w])",
    re.IGNORECASE)
MULTIPLIER = {"k": 1_000, "m": 1_000_000, "mill": 1_000_000, "million": 1_000_000}


# ---------- what the cited fields hold ----------

def flatten(value) -> list:
    """All numbers and strings inside a value (dicts and lists included)."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, dict):
        return [x for v in value.values() for x in flatten(v)]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in flatten(v)]
    return [value]


def _numbers_and_dates(values) -> tuple[list[float], set[tuple[int, int]]]:
    numbers, dates = [], set()
    for v in flatten(values):
        if isinstance(v, (int, float)):
            numbers.append(float(v))
        elif isinstance(v, str):
            for y, m, d in ISO_DATE.findall(v):
                dates.add((int(m), int(d)))
    return numbers, dates


# ---------- what the claim says ----------

def _month(text: str) -> int:
    return MONTHS[text.lower()[:3]]


def claim_dates(text: str) -> tuple[list[tuple[int, int, str]], str]:
    """(month, day, as written) for every date in the text, and the text with the dates removed."""
    found = []

    def iso(m):
        found.append((int(m.group(2)), int(m.group(3)), m.group(0)))
        return " "

    def day_month(m):
        for day in filter(None, (m.group(1), m.group(2))):
            found.append((_month(m.group(3)), int(day), m.group(0)))
        return " "

    def month_day(m):
        for day in filter(None, (m.group(2), m.group(3))):
            found.append((_month(m.group(1)), int(day), m.group(0)))
        return " "

    text = ISO_DATE.sub(iso, text)
    text = DAY_MONTH.sub(day_month, text)
    text = MONTH_DAY.sub(month_day, text)
    return found, text


def claim_numbers(text: str) -> list[tuple[float, float, bool, str]]:
    """(value, precision step, is_percent, as written) for every number in the text (dates/years removed)."""
    out = []
    for m in NUMBER.finditer(text):
        whole, sep, decimals, suffix = m.group(1), m.group(2), m.group(3), (m.group(4) or "").lower()
        whole = re.sub(r"[,  ]", "", whole)
        if sep == "," and decimals and len(decimals) == 3:      # "1,234" was caught as whole above; safety
            whole, decimals = whole + decimals, None
        value = float(f"{whole}.{decimals}" if decimals else whole)
        step = 10 ** -(len(decimals) if decimals else 0)
        if not decimals and value >= 1000:      # "49,000" is a rounded 49,235: trailing zeros set the precision
            zeros = len(whole) - len(whole.rstrip("0"))
            step = 10 ** min(zeros, len(whole) - 1)
        mult = MULTIPLIER.get(suffix, 1)
        out.append((value * mult, step * mult, suffix in ("%", "percent", "pct"), m.group(0).strip()))
    return out


def strip_names(text: str, names) -> str:
    """Remove names that contain digits ("Røkt laks 300 g") so their digits aren't read as claims."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


# ---------- the check ----------

def _close(candidate: float, value: float, step: float) -> bool:
    return abs(abs(candidate) - value) <= step / 2 + 1e-9


def claim_mismatches(text: str, values, names=()) -> list[str]:
    """Numbers and dates in `text` that none of the cited `values` explain (empty list = all fine)."""
    numbers, dates = _numbers_and_dates(values)
    found_dates, rest = claim_dates(strip_names(text, names))
    rest = YEAR.sub(" ", rest)
    problems = [f'date "{shown}"' for month, day, shown in found_dates if (month, day) not in dates]

    differences = [abs(a - b) for a, b in combinations(numbers, 2)]
    for value, step, percent, shown in claim_numbers(rest):
        if percent:
            candidates = [n * 100 for n in numbers] + [(n - 1) * 100 for n in numbers] + numbers
        else:
            candidates = numbers + differences
        if not any(_close(c, value, step) for c in candidates):
            problems.append(f'number "{shown}"')
    return list(dict.fromkeys(problems))   # unique, in order

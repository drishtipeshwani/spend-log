"""Reference parser for the AU Bank Ixigo RuPay statement, used only to check Gemma's extraction.

It reads the layout by word position, so its rows are exact for this one layout. Normal runs use
Gemma for every bank (app/extraction.py); this gives tests a known-good answer to compare against.

Each transaction is a small block rather than a table row:

          UPI/ZOMATO IND                      <- description
      28                          ₹250.00     <- day (left), amount (right edge; "+₹" = credit)
     Aug 26 Dr 5 RP                           <- month, 2-digit year, Dr/Cr, reward points

The parser anchors on each amount at the right edge, then reads the words on its row and the line
below by position. Example amounts in the fee pages have no day/month around them, so they're skipped.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pdfplumber

from app.transactions import ParsedStatement, StatementSummary, Transaction

BANK = "AU Bank"
CARD = "Ixigo RuPay"

AMOUNT = re.compile(r"^(\+?)₹([\d,]+\.\d{2})$")
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def matches(meta: dict) -> bool:
    return "aubank.in" in meta.get("sender", "").lower() and "ixigo" in (
        meta.get("subject", "") + meta.get("pdf", "")).lower()


def _money(s: str) -> float:
    return float(s.replace("₹", "").replace(",", "").lstrip("+"))


def _date(s: str) -> date | None:
    try:
        return datetime.strptime(" ".join(s.split()), "%d %b %Y").date()
    except ValueError:
        return None


def _find_phrase(words: list[dict], phrase: str) -> dict | None:
    """First occurrence of a multi-word phrase; returns a box spanning it."""
    parts = phrase.split()
    for i in range(len(words) - len(parts) + 1):
        run = words[i:i + len(parts)]
        if [w["text"] for w in run] == parts and len({round(w["top"]) for w in run}) == 1:
            return {"x0": run[0]["x0"], "x1": run[-1]["x1"], "top": run[0]["top"], "bottom": run[0]["bottom"]}
    return None


def _value_right_of(words, label) -> float | None:
    """The ₹ amount on the same line as a label, to its right."""
    hits = [w for w in words if abs(w["top"] - label["top"]) < 4 and w["x0"] > label["x1"] and AMOUNT.match(w["text"])]
    return _money(min(hits, key=lambda w: w["x0"])["text"]) if hits else None


def _value_below(words, label) -> float | None:
    """The ₹ amount on the next line down, starting in the same column as a label."""
    hits = [w for w in words if 0 < w["top"] - label["bottom"] < 30 and abs(w["x0"] - label["x0"]) < 25
            and AMOUNT.match(w["text"])]
    return _money(min(hits, key=lambda w: w["top"])["text"]) if hits else None


def _summary(page) -> StatementSummary:
    words = page.extract_words()
    text = page.extract_text() or ""
    s = StatementSummary()

    # "Statement for your credit card ending with 1234 (27 Aug - 26 Sep 2026)"
    m = re.search(r"(\d{4})\s*\((\d{1,2}\s+\w{3})\s*-\s*(\d{1,2}\s+\w{3}\s+\d{4})\)", text)
    if m:
        s.card_last4 = m.group(1)
        s.period_end = _date(m.group(3))
        if s.period_end:
            start = _date(f"{m.group(2)} {s.period_end.year}")
            if start and start > s.period_end:  # period crosses a new year
                start = start.replace(year=start.year - 1)
            s.period_start = start

    m = re.search(r"Payment due date\s+(\d{1,2}\s+\w{3}\s+\d{4})", text)
    if m:
        s.due_date = _date(m.group(1))

    if label := _find_phrase(words, "Total bill amount"):
        s.total_due = _value_below(words, label)
    if s.total_due is None and (label := _find_phrase(words, "Total amount due")):
        s.total_due = _value_right_of(words, label)
    if label := _find_phrase(words, "Minimum amount due"):
        s.min_due = _value_below(words, label)
    if label := _find_phrase(words, "Total spends"):
        s.total_spends = _value_right_of(words, label)
    if label := _find_phrase(words, "Payments &"):
        s.payments_credits = _value_right_of(words, label)
    return s


def _page_transactions(page, problems: list[str]) -> tuple[list[Transaction], int]:
    words = page.extract_words()
    header = _find_phrase(words, "Your Transactions")
    if not header:
        return [], 0
    footer_top = min((w["top"] for w in words if w["top"] > header["bottom"]
                      and w["text"] in ("Reward",) and w["x0"] < 60), default=page.height)
    edge = page.width * 0.75
    anchors = [w for w in words if AMOUNT.match(w["text"]) and w["x0"] > edge
               and header["bottom"] < w["top"] < footer_top]

    rows = []
    for a in anchors:
        sign, amount = AMOUNT.match(a["text"]).groups()
        same_row = [w for w in words if -15 < w["top"] - a["top"] < 4 and w is not a]
        below = sorted((w for w in words if 10 < w["top"] - a["top"] < 25 and w["x0"] < edge),
                       key=lambda w: w["x0"])
        day = next((w["text"] for w in same_row if w["x1"] < 60 and w["text"].isdigit()), None)
        desc = " ".join(w["text"] for w in sorted(same_row, key=lambda w: (round(w["top"]), w["x0"]))
                        if 60 <= w["x0"] < edge)
        below_text = [w["text"] for w in below]

        if not (day and len(below_text) >= 3 and below_text[0] in MONTHS and below_text[1].isdigit()):
            problems.append(f"page {page.page_number}: couldn't read the date around {a['text']}")
            continue
        when = date(2000 + int(below_text[1]), MONTHS[below_text[0]], int(day))
        dr_cr = below_text[2]
        direction = "credit" if sign == "+" else "debit"
        if dr_cr not in ("Dr", "Cr") or (dr_cr == "Cr") != (direction == "credit"):
            problems.append(f"page {page.page_number}: {a['text']} on {when} is marked {dr_cr}")
        points = 0
        if "RP" in below_text:
            i = below_text.index("RP")
            points = int(below_text[i - 1]) if i > 0 and below_text[i - 1].isdigit() else 0
        rows.append(Transaction(date=when, description=desc, amount=_money(amount),
                                direction=direction, reward_points=points))
    return rows, len(anchors)


def parse(pdf_path: Path) -> ParsedStatement:
    problems: list[str] = []
    transactions, seen = [], 0
    with pdfplumber.open(pdf_path) as pdf:
        summary = _summary(pdf.pages[0])
        for page in pdf.pages:
            rows, n = _page_transactions(page, problems)
            transactions += rows
            seen += n
    if len(transactions) != seen:
        problems.append(f"{seen} amounts in the transactions section but {len(transactions)} rows parsed")
    return ParsedStatement(bank=BANK, card=CARD, summary=summary, transactions=transactions, problems=problems)

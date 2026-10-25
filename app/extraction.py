"""Turn any bank's statement into transactions with a local model, then check the answer.

Gemma reads the column-aligned text of each page (never the PDF itself) and returns rows in a fixed
JSON shape. Python doesn't trust those rows: every amount must be printed on that page, no amount can
be used more often than it appears, dates must fall in the statement period, and debits and credits
must add up to the totals printed on the statement. Pages that fail are retried once, with a hint
listing the amounts that weren't accounted for.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

from app import llm
from app.pdf_reader import layout_text
from app.transactions import ParsedStatement, StatementSummary, Transaction, check

# 1,063.00 / 16,914.82 / 1,23,456.78 / 450.00, with or without ₹, Rs, Rs. or a sign in front.
# Not preceded by a digit, a comma, or a digit-then-dot (so "Rs.645.36" counts but ".36" in "645.36" doesn't).
_AMOUNT = re.compile(r"(?<![\d,])(?<!\d\.)(?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2}(?![\d])")


def amounts_on(text: str) -> Counter:
    return Counter(round(float(a.replace(",", "")), 2) for a in _AMOUNT.findall(text))


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_money(value) -> float | None:
    try:
        return round(float(str(value).replace(",", "").replace("₹", "").strip()), 2)
    except ValueError:
        return None


# --- Statement summary ---------------------------------------------------------------------

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in (
        "bank", "card", "card_last4", "period_start", "period_end", "due_date",
        "total_due", "min_due", "total_spends", "payments_credits")},
    "required": ["bank", "card", "card_last4", "period_start", "period_end", "due_date",
                 "total_due", "min_due", "total_spends", "payments_credits"],
}

_SUMMARY_SYSTEM = """You read the first page of an Indian credit card statement and fill in its \
summary. Use "" for anything not printed on the page; never guess.
- bank: the card issuer's name as printed at the top of the statement, e.g. "HDFC Bank", "AU Bank", \
"SBI Card", "Amex". Issuer names can end in "Card". Never take it from example text or a password hint.
- card: the card's product name only, e.g. "Regalia Gold", "Ixigo RuPay", "Atlas", from the page or the \
email subject, without the words "Credit Card". "" if neither names it.
- card_last4: last 4 digits of the card number.
- period_start, period_end, due_date: YYYY-MM-DD. If the start date has no year, use the year that \
makes the period end on period_end.
- total_due, min_due: the total amount due and minimum amount due, digits only, e.g. "16914.82".
- total_spends: purchases/debits/spends this cycle. payments_credits: payments and credits this \
cycle. Digits only, from the statement's summary, not computed."""


def read_summary(first_page: str, subject: str = "") -> StatementSummary:
    user = (f"Email subject: {subject}\n\n" if subject else "") + f"First page:\n{first_page}"
    raw = llm.chat_json(_SUMMARY_SYSTEM, user, _SUMMARY_SCHEMA)
    printed = amounts_on(first_page)

    def money(key: str) -> float | None:
        value = _parse_money(raw.get(key, "")) if raw.get(key) else None
        return value if value is not None and value in printed else None  # only amounts on the page

    last4 = re.sub(r"\D", "", raw.get("card_last4", ""))[-4:]
    seen = f"{first_page}\n{subject}".lower()

    def name(key: str) -> str:
        """Bank or card name, only if it's actually printed on the page or in the subject."""
        value = re.sub(r"\s+(credit\s+)?card$", "", raw.get(key, "").strip(), flags=re.I) if key == "card" \
            else raw.get(key, "").strip()
        return value if value and value.lower() in seen else ""

    return StatementSummary(
        bank=name("bank"), card=name("card"),
        card_last4=last4 if len(last4) == 4 and last4 in first_page else "",
        period_start=_parse_date(raw.get("period_start", "")), period_end=_parse_date(raw.get("period_end", "")),
        due_date=_parse_date(raw.get("due_date", "")),
        total_due=money("total_due"), min_due=money("min_due"),
        total_spends=money("total_spends"), payments_credits=money("payments_credits"),
    )


# --- Transactions --------------------------------------------------------------------------

_ROWS_SCHEMA = {
    "type": "object",
    "properties": {"transactions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "description": {"type": "string"},
            "amount": {"type": "number"},
            "direction": {"type": "string", "enum": ["debit", "credit"]},
            "reward_points": {"type": "integer"},
        },
        "required": ["date", "description", "amount", "direction", "reward_points"]}}},
    "required": ["transactions"],
}

_ROWS_SYSTEM = """You extract transactions from one page of an Indian credit card statement. The \
page text keeps the PDF's layout: columns are lined up with spaces, and one transaction can span \
several lines.

Return every transaction on the page, in order:
- date: YYYY-MM-DD. Use the statement period to work out the year when it isn't printed in full. \
In block layouts the day is the number on the same line as the amount, and a line like "Sep 26" is \
the month and a two-digit year (September 2026), not the day.
- description: the merchant/description text as printed. Join it into one line if it wraps.
- amount: a positive number, exactly as printed (no rounding).
- direction: "credit" for payments received, refunds, reversals and cashback, or when marked Cr, \
CR, "+" or in a credit column. Otherwise "debit".
- reward_points: points shown for that transaction, else 0.

Only real transactions. Skip summary boxes (opening balance, total due, minimum due, credit limit), \
reward point tables, EMI schedules, totals, and example amounts in terms and conditions or fee \
explanations. If the page has no transactions, return {"transactions": []}."""

_EXAMPLES = """Example page (block layout):
    Your Transactions
          UPI/SWIGGY IND
      06                                    ₹203.00
     Sep 26 Dr 5 RP
          PYMNT RCV BBPS
      14                                 +₹2,500.00
     Sep 26 Cr
Example answer:
{"transactions": [{"date": "2026-09-06", "description": "UPI/SWIGGY IND", "amount": 203.0, "direction": "debit", "reward_points": 5}, {"date": "2026-09-14", "description": "PYMNT RCV BBPS", "amount": 2500.0, "direction": "credit", "reward_points": 0}]}

Example page (table layout):
    Date        Transaction Details                    Amount (Rs.)
    02/09/2026  AMAZON PAY INDIA PRIVATE LIMITED       1,299.00
                MUMBAI
    10/09/2026  PAYMENT RECEIVED - THANK YOU          12,000.00 Cr
Example answer:
{"transactions": [{"date": "2026-09-02", "description": "AMAZON PAY INDIA PRIVATE LIMITED MUMBAI", "amount": 1299.0, "direction": "debit", "reward_points": 0}, {"date": "2026-09-10", "description": "PAYMENT RECEIVED - THANK YOU", "amount": 12000.0, "direction": "credit", "reward_points": 0}]}"""


def _ask_rows(page_text: str, period: str, hint: str = "") -> list[dict]:
    user = f"{_EXAMPLES}\n\nStatement period: {period}\n{hint}\nPage:\n{page_text}\nAnswer:"
    return llm.chat_json(_ROWS_SYSTEM, user, _ROWS_SCHEMA, timeout=300).get("transactions", [])


def _validate(raw_rows: list[dict], page_text: str, summary: StatementSummary) -> tuple[list[Transaction], list[str]]:
    """Keep rows whose amount is printed on the page (no more often than printed) and whose date is
    plausible. Returns (good rows, reasons for anything dropped)."""
    available = amounts_on(page_text)
    lo = summary.period_start - timedelta(days=5) if summary.period_start else None
    hi = summary.period_end if summary.period_end else None
    good, dropped = [], []
    for r in raw_rows:
        amount = _parse_money(r.get("amount"))
        when = _parse_date(r.get("date", ""))
        desc = " ".join(str(r.get("description", "")).split())
        if amount is None or amount <= 0 or available[amount] <= 0:
            dropped.append(f"amount {r.get('amount')} isn't on the page")
            continue
        if when is None or (lo and when < lo) or (hi and when > hi):
            dropped.append(f"date {r.get('date')} isn't in the statement period")
            continue
        if not desc or r.get("direction") not in ("debit", "credit"):
            dropped.append(f"row for {amount} is incomplete")
            continue
        available[amount] -= 1
        points = r.get("reward_points")
        good.append(Transaction(date=when, description=desc, amount=amount, direction=r["direction"],
                                reward_points=points if isinstance(points, int) and points >= 0 else 0))
    return good, dropped


def _unclaimed(page_text: str, rows: list[Transaction]) -> list[float]:
    left = amounts_on(page_text) - Counter(t.amount for t in rows)
    return sorted(left.elements())


def page_texts(pdf_path: Path) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [layout_text(page) for page in pdf.pages]


def extract_statement(pdf_path: Path, subject: str = "", log=print) -> ParsedStatement:
    """Raises llm.LLMUnavailable if Ollama can't be reached. `subject` is the email's subject line."""
    pages = page_texts(pdf_path)
    summary = read_summary(pages[0], subject)
    period = (f"{summary.period_start} to {summary.period_end}" if summary.period_start and summary.period_end
              else "unknown")

    per_page: dict[int, list[Transaction]] = {}
    problems: list[str] = []
    for n, text in enumerate(pages, 1):
        if not amounts_on(text):
            continue  # nothing that could be a transaction
        started = time.monotonic()
        rows, dropped = _validate(_ask_rows(text, period), text, summary)
        if dropped:  # retry once, telling the model what went wrong
            hint = "Previous answer had problems: " + "; ".join(dropped) + ". Use amounts exactly as printed.\n"
            rows, dropped = _validate(_ask_rows(text, period, hint), text, summary)
            problems += [f"page {n}: {d}" for d in dropped]
        per_page[n] = rows
        log(f"   page {n}: {len(rows)} transaction(s) ({time.monotonic() - started:.0f}s)")

    parsed = ParsedStatement(bank=summary.bank, card=summary.card, summary=summary,
                             transactions=[t for n in sorted(per_page) for t in per_page[n]], problems=problems)

    # Totals don't match: retry pages that have amounts nobody claimed, listing them as a hint.
    if any("add up to" in p for p in check(parsed)):
        for n, rows in per_page.items():
            missing = _unclaimed(pages[n - 1], rows)
            if not missing:
                continue
            hint = ("These amounts on the page are not in your answer: " + ", ".join(f"{a:.2f}" for a in missing)
                    + ". Include any that are transactions; leave out summary or example amounts.\n")
            retry, _ = _validate(_ask_rows(pages[n - 1], period, hint), pages[n - 1], summary)
            if len(retry) > len(rows):
                per_page[n] = retry
                log(f"   page {n}: retried, {len(retry)} transaction(s)")
        parsed.transactions = [t for n in sorted(per_page) for t in per_page[n]]
    return parsed

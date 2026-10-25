"""Shared types for extracted statements, and the checks every statement goes through."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Transaction:
    date: date
    description: str
    amount: float           # always positive
    direction: str          # "debit" or "credit"
    reward_points: int = 0


@dataclass
class StatementSummary:
    bank: str = ""
    card: str = ""
    card_last4: str = ""
    period_start: date | None = None
    period_end: date | None = None
    total_due: float | None = None
    min_due: float | None = None
    due_date: date | None = None
    total_spends: float | None = None      # debits this cycle, as printed on the statement
    payments_credits: float | None = None  # credits this cycle, as printed on the statement


@dataclass
class ParsedStatement:
    bank: str
    card: str
    summary: StatementSummary
    transactions: list[Transaction]
    problems: list[str] = field(default_factory=list)


def check(parsed: ParsedStatement) -> list[str]:
    """Return a list of problems; empty means the statement passed every check."""
    problems = list(parsed.problems)
    txns, s = parsed.transactions, parsed.summary

    if s.period_start and s.period_end:
        lo, hi = s.period_start - timedelta(days=5), s.period_end
        outside = [t for t in txns if not lo <= t.date <= hi]
        if outside:
            problems.append(f"{len(outside)} row(s) dated outside {s.period_start} to {s.period_end}")

    def close(a: float, b: float) -> bool:
        return abs(a - b) < 0.01

    debits = sum(t.amount for t in txns if t.direction == "debit")
    credits = sum(t.amount for t in txns if t.direction == "credit")
    if s.total_spends is not None and not close(debits, s.total_spends):
        problems.append(f"debits add up to {debits:.2f}, statement says total spends {s.total_spends:.2f}")
    if s.payments_credits is not None and not close(credits, s.payments_credits):
        problems.append(f"credits add up to {credits:.2f}, statement says payments/credits {s.payments_credits:.2f}")
    return problems

"""What counts as spend, and how money is shown. Shared by spend_report.py and the dashboard."""

from __future__ import annotations

from app.categories import NOT_SPEND


def inr(amount: float, decimals: int = 2) -> str:
    """Indian digit grouping: 123456.5 -> ₹1,23,456.50"""
    whole, _, frac = f"{abs(amount):.{decimals}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if amount < 0 else "") + "₹" + ",".join(groups + [tail]) + (f".{frac}" if frac else "")


def inr_short(amount: float) -> str:
    """Compact Indian units for headline numbers: ₹2.4 L (lakh), ₹39.9 K, ₹1.2 Cr."""
    sign, a = ("-" if amount < 0 else ""), abs(amount)
    for size, unit in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if a >= size:
            return f"{sign}₹{a / size:.1f} {unit}".replace(".0 ", " ")
    return f"{sign}₹{a:.0f}"


def spend_of(category: str, direction: str, amount: float) -> float:
    """What a transaction adds to spend: debits count, refunds subtract, card payments are ignored."""
    category = category or "Uncategorized"
    if category in NOT_SPEND:
        return 0.0
    if direction == "debit":
        return amount
    return -amount if category == "Refund" else 0.0

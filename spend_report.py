"""Where did the money go, and how much? Reads data/transactions.csv.

    python spend_report.py                       # whole family, all months
    python spend_report.py --holders mom         # one person
    python spend_report.py --holders mom,dad     # a group
    python spend_report.py --month 2026-09 --top 5

Spend counts debits, minus refunds. Card payments are left out: they're money paid to the card,
not money spent.
"""

import argparse
import sys
from collections import defaultdict

from app.categories import NOT_SPEND
from app.csv_store import TRANSACTIONS, read_rows
from app.spend import inr, spend_of


def table(title: str, totals: dict[str, float], counts: dict[str, int], grand: float, top: int | None = None):
    rows = sorted(((k, v) for k, v in totals.items() if v), key=lambda kv: -kv[1])
    if top:
        rows = rows[:top]
    if not rows:
        return
    width = max(len(k) for k, _ in rows)
    print(f"\n{title}")
    for name, amount in rows:
        share = amount / grand if grand else 0
        bar = "█" * max(1, round(share * 30)) if amount > 0 else ""
        print(f"  {name:<{width}}  {inr(amount):>14}  {share:6.1%}  {counts[name]:>3} txn  {bar}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Where the money went, for one person or a group.")
    parser.add_argument("--holders", help="Comma-separated people, e.g. mom or mom,dad (default: everyone)")
    parser.add_argument("--month", help="Only this month, YYYY-MM")
    parser.add_argument("--top", type=int, default=10, help="How many merchants to list (default 10)")
    args = parser.parse_args()

    txns = read_rows(TRANSACTIONS)
    if not txns:
        print("No transactions yet. Run: python build_transactions.py")
        return 1

    holders = {h.strip().lower() for h in args.holders.split(",")} if args.holders else None
    if holders:
        txns = [t for t in txns if t["holder"].lower() in holders]
    if args.month:
        txns = [t for t in txns if t["date"].startswith(args.month)]
    if not txns:
        print("No transactions match those filters.")
        return 0

    by: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in txns:
        amount = spend_of(t["category"], t["direction"], float(t["amount"]))
        if not amount:
            continue
        card = f"{t['bank']} {t['card']}" + (f" ··{t['card_last4']}" if t["card_last4"] else "")
        for dim, key in (("category", t["category"] or "Uncategorized"), ("merchant", t["merchant"] or t["description"]),
                         ("person", t["holder"]), ("card", card), ("month", t["date"][:7])):
            by[dim][key] += amount
            counts[dim][key] += 1

    total = sum(by["person"].values())
    who = ", ".join(sorted(holders)) if holders else "whole family"
    dates = sorted(t["date"] for t in txns)
    print(f"Spend for {who}, {dates[0]} to {dates[-1]}")
    print(f"Total spent: {inr(total)}")
    payments = sum(float(t["amount"]) for t in txns if t["category"] in NOT_SPEND)
    if payments:
        print(f"Paid to cards: {inr(payments)} (not counted as spend)")

    table("By category", by["category"], counts["category"], total)
    table(f"Top merchants", by["merchant"], counts["merchant"], total, top=args.top)
    if len(by["person"]) > 1 or not holders:
        table("By person", by["person"], counts["person"], total)
    table("By card", by["card"], counts["card"], total)
    table("By month", by["month"], counts["month"], total)

    uncategorized = by["category"].get("Uncategorized")
    if uncategorized:
        print(f"\n{inr(uncategorized)} is uncategorized. Run build_transactions.py with Ollama running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

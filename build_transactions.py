"""Turn unlocked statement PDFs into data/transactions.csv, statements.csv and merchants.csv.

Works from data/<account>/unlocked/ and meta/, so it needs no Gmail or passwords. The local model
(Gemma via Ollama) reads each statement's transactions and categorizes new merchants, so Ollama must
be running. Statements already in statements.csv are skipped unless --force.

    python build_transactions.py
    python build_transactions.py --force      # re-read every statement
    python build_transactions.py --no-llm     # skip new statements; only re-apply merchants.csv
    python build_transactions.py --recategorize  # redo the model's categories, keep your edits
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from app import llm
from app.extraction import extract_statement
from app.categories import CATEGORIES, apply_categories, categorize, merchant_key, rule_category, same_brand
from app.csv_store import (MERCHANTS, MERCHANT_FIELDS, STATEMENTS, STATEMENT_FIELDS, TRANSACTIONS,
                           TRANSACTION_FIELDS, read_rows, write_rows)
from app.storage import AccountDirs
from app.transactions import check


def statement_id(meta: dict) -> str:
    return f"{meta['received_on']}-{hashlib.sha1(meta['pdf'].encode()).hexdigest()[:6]}"


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{value:.2f}" if isinstance(value, float) else str(value)


_PERSONAL_MAIL = {"gmail", "googlemail", "yahoo", "outlook", "hotmail", "live", "icloud", "me", "proton",
                  "protonmail", "rediffmail", "aol", "zoho", "example"}


def _bank_from_sender(meta: dict) -> str:
    """The bank from the sender's domain (alerts@hdfcbank.net -> HDFCBANK), unless it's a personal address."""
    domain = (re.findall(r"@([\w.-]+)", meta.get("sender", "")) or [""])[0]
    name = domain.split(".")[0].lower()
    return name.upper() if name and name not in _PERSONAL_MAIL else "Unknown bank"


def harmonize_cards(txns: list[dict], stmts: dict[str, dict]) -> None:
    """Statements of one card (same person and last 4 digits) get one bank/card name: the most common
    reading. Stops a single odd reading from splitting a card in two on the dashboard."""
    from collections import Counter
    groups: dict[tuple, list[dict]] = {}
    for s in stmts.values():
        if s["card_last4"]:
            groups.setdefault((s["holder"], s["card_last4"]), []).append(s)
    for group in groups.values():
        names = Counter((s["bank"], s["card"]) for s in group if s["bank"] != "Unknown bank")
        if not names:
            continue
        bank, card = max(names.items(), key=lambda kv: (kv[1], bool(kv[0][1])))[0]
        ids = {s["statement_id"] for s in group}
        for s in group:
            s["bank"], s["card"] = bank, card
        for t in txns:
            if t["statement_id"] in ids:
                t["bank"], t["card"] = bank, card


def parse_statements(accounts: list[str], force: bool, txns: list[dict], stmts: dict[str, dict],
                     use_llm: bool = True) -> int:
    """Read new statements into txns/stmts in place. Returns how many were read."""
    parsed_count = 0
    for account in accounts:
        dirs = AccountDirs.for_account(account)
        for meta_path in sorted(dirs.meta.glob("*.json")):
            meta = json.loads(meta_path.read_text())
            sid = statement_id(meta)
            if sid in stmts and not force:
                continue
            pdf = dirs.unlocked / meta["pdf"]
            if not pdf.exists():
                continue
            if not use_llm:
                print(f"{meta['received_on']}  {meta['subject']}\n   Not read yet (--no-llm)\n")
                continue

            print(f"{meta['received_on']}  {meta['subject']}")
            try:
                parsed = extract_statement(pdf, subject=meta.get("subject", ""))
            except llm.LLMUnavailable as e:
                print(f"   Model unavailable, nothing written for this statement: {e}")
                print("   Start Ollama and run build_transactions.py again.\n")
                return parsed_count  # later statements would fail the same way
            parsed.bank = parsed.bank or _bank_from_sender(meta)
            problems = check(parsed)
            s = parsed.summary
            holder = meta.get("holder") or "unknown"
            card_last4 = s.card_last4 or next(iter(meta.get("cards_in_email") or []), "")

            txns[:] = [t for t in txns if t["statement_id"] != sid]
            for i, t in enumerate(parsed.transactions, 1):
                txns.append({
                    "txn_id": f"{sid}-{i:03d}", "statement_id": sid, "holder": holder,
                    "bank": parsed.bank, "card": parsed.card, "card_last4": card_last4,
                    "date": t.date.isoformat(), "description": t.description, "merchant": "",
                    "amount": f"{t.amount:.2f}", "direction": t.direction,
                    "reward_points": t.reward_points, "category": "", "category_source": "",
                })
            stmts[sid] = {
                "statement_id": sid, "account": account, "holder": holder, "bank": parsed.bank,
                "card": parsed.card, "card_last4": card_last4, "period_start": _fmt(s.period_start),
                "period_end": _fmt(s.period_end), "total_due": _fmt(s.total_due), "min_due": _fmt(s.min_due),
                "due_date": _fmt(s.due_date), "row_count": len(parsed.transactions),
                "checks": "; ".join(problems) or "ok", "pdf": meta["pdf"],
            }
            parsed_count += 1
            period = f"{s.period_start} to {s.period_end}" if s.period_start else "period not found"
            print(f"   {parsed.bank} {parsed.card} ({holder}), {period}: {len(parsed.transactions)} transactions")
            print("   Checks passed" if not problems else "   CHECK FAILED: " + "\n   CHECK FAILED: ".join(problems))
            print()
    return parsed_count


def categorize_merchants(txns: list[dict], merchants: dict[str, dict], use_llm: bool) -> None:
    """Add merchants.csv entries for any new merchant keys (rules first, then the model)."""
    todo: dict[str, tuple[str, str, str]] = {}
    # A category that's no longer on the list (e.g. after "Entertainment & subscriptions" was split)
    # gets asked again, unless you set it yourself.
    for key in [k for k, m in merchants.items() if m["category"] not in CATEGORIES and m["category_source"] != "user"]:
        del merchants[key]
    for t in txns:
        key = merchant_key(t["description"])
        if key in merchants or key in todo:
            continue
        if rule := rule_category(t["description"], t["direction"]):
            merchants[key] = {"key": key, "merchant": rule[0], "category": rule[1], "category_source": "rule"}
        else:
            todo[key] = (key, t["description"], t["direction"])

    if not todo:
        return
    if not use_llm:
        print(f"{len(todo)} new merchant(s) left uncategorized (--no-llm)")
        return
    print(f"Categorizing {len(todo)} new merchant(s) with {llm.MODEL}...")
    known = [m["merchant"] for m in merchants.values() if m["category_source"] != "rule"]
    try:
        answers = categorize(list(todo.values()), known=known)
    except llm.LLMUnavailable as e:
        print(f"   Model unavailable, {len(todo)} merchant(s) left uncategorized: {e}")
        return
    for key, (merchant, category) in answers.items():
        if merchant in known and not same_brand(merchant, key):  # a different brand it already knew
            merchant = key.title()
        merchants[key] = {"key": key, "merchant": merchant, "category": category, "category_source": "model"}
    missed = len(todo) - len(answers)
    if missed:
        print(f"   {missed} merchant(s) the model didn't answer; re-run to retry")


def run(force: bool = False, use_llm: bool = True, accounts: list[str] | None = None,
        recategorize: bool = False) -> None:
    txns = read_rows(TRANSACTIONS)
    stmts = {s["statement_id"]: s for s in read_rows(STATEMENTS)}
    merchants = {m["key"]: m for m in read_rows(MERCHANTS)}
    if recategorize:  # redo the model's answers; your edits ("user") and rules stay
        merchants = {k: m for k, m in merchants.items() if m["category_source"] != "model"}

    parsed = parse_statements(accounts or AccountDirs.accounts(), force, txns, stmts, use_llm)
    harmonize_cards(txns, stmts)
    categorize_merchants(txns, merchants, use_llm)
    apply_categories(txns, merchants)

    txns.sort(key=lambda t: (t["date"], t["txn_id"]))
    write_rows(TRANSACTIONS, TRANSACTION_FIELDS, txns)
    write_rows(STATEMENTS, STATEMENT_FIELDS, sorted(stmts.values(), key=lambda s: s["statement_id"]))
    write_rows(MERCHANTS, MERCHANT_FIELDS, sorted(merchants.values(), key=lambda m: m["key"]))

    uncategorized = sum(1 for t in txns if not t["category"])
    print(f"Done. {parsed} statement(s) parsed. {len(txns)} transactions in data/transactions.csv"
          + (f", {uncategorized} uncategorized" if uncategorized else "") + ".")



def main() -> int:
    parser = argparse.ArgumentParser(description="Build transactions/statements/merchants CSVs from unlocked PDFs.")
    parser.add_argument("--force", action="store_true", help="Re-read statements already in statements.csv")
    parser.add_argument("--no-llm", action="store_true", help="Don't call the model: skip new statements, leave new merchants uncategorized")
    parser.add_argument("--recategorize", action="store_true",
                        help="Ask the model again for every merchant it categorized (keeps your edits)")
    parser.add_argument("--account", help="Only this account (default: all)")
    args = parser.parse_args()
    run(force=args.force, use_llm=not args.no_llm, accounts=[args.account] if args.account else None,
        recategorize=args.recategorize)
    return 0


if __name__ == "__main__":
    sys.exit(main())

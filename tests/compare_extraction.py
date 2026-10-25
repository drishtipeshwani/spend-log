"""Check Gemma's extraction against the reference parser on AU Bank Ixigo statements.

    python tests/compare_extraction.py                    # every unlocked Ixigo statement
    SPEND_LOG_MODEL=gemma4:12b python tests/compare_extraction.py

Prints how many rows match exactly (date, amount, direction, reward points, description), and the
differences. Needs Ollama running and statements already downloaded.
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.extraction import extract_statement  # noqa: E402
from app.storage import AccountDirs  # noqa: E402
from app.transactions import check  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_au_ixigo  # noqa: E402


def key(t) -> tuple:
    return (t.date.isoformat(), t.amount, t.direction, t.reward_points, " ".join(t.description.split()))


def main() -> int:
    statements = [(d.unlocked / m["pdf"], m) for account in AccountDirs.accounts()
                  for d in [AccountDirs.for_account(account)]
                  for m in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.meta.glob("*.json")))
                  if reference_au_ixigo.matches(m) and (d.unlocked / m["pdf"]).exists()]
    if not statements:
        print("No unlocked AU Ixigo statements found.")
        return 1

    print(f"Model: {llm.MODEL}")
    all_ok = True
    for pdf, meta in statements:
        print(f"\n{meta['received_on']}  {meta['subject']}")
        expected = reference_au_ixigo.parse(pdf)
        started = time.monotonic()
        got = extract_statement(pdf, subject=meta['subject'])
        elapsed = time.monotonic() - started

        want, have = Counter(map(key, expected.transactions)), Counter(map(key, got.transactions))
        matched = sum((want & have).values())
        print(f"   {matched}/{len(expected.transactions)} rows match exactly, {len(got.transactions)} returned, "
              f"{elapsed:.0f}s")
        for k in (want - have).elements():
            print(f"   missing:   {k}")
        for k in (have - want).elements():
            print(f"   unexpected:{k}")

        es, gs = expected.summary, got.summary
        for field in ("card_last4", "period_start", "period_end", "due_date", "total_due", "min_due",
                      "total_spends", "payments_credits"):
            if getattr(es, field) != getattr(gs, field):
                print(f"   summary {field}: expected {getattr(es, field)}, got {getattr(gs, field)}")
        print(f"   bank/card read as: {gs.bank!r} / {gs.card!r}")
        print(f"   checks: {check(got) or 'all passed'}")
        all_ok &= matched == len(expected.transactions) == len(got.transactions)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

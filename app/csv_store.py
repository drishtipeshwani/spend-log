"""The three CSV files everything else reads: transactions, statements, merchants.

Plain CSV so they open in a spreadsheet, load straight into pandas (TabPFN), and can be queried
with SQL later (DuckDB reads CSV directly). All live in data/ (gitignored, file mode 600).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from app.storage import DATA_DIR, write_private

TRANSACTIONS = DATA_DIR / "transactions.csv"
STATEMENTS = DATA_DIR / "statements.csv"
MERCHANTS = DATA_DIR / "merchants.csv"

TRANSACTION_FIELDS = ["txn_id", "statement_id", "holder", "bank", "card", "card_last4", "date",
                      "description", "merchant", "amount", "direction", "reward_points",
                      "category", "category_source"]
STATEMENT_FIELDS = ["statement_id", "account", "holder", "bank", "card", "card_last4", "period_start",
                    "period_end", "total_due", "min_due", "due_date", "row_count", "checks", "pdf"]
MERCHANT_FIELDS = ["key", "merchant", "category", "category_source"]


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    write_private(path, buf.getvalue())

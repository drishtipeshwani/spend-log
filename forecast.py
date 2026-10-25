"""Forecast each card's next statement (TabPFN) and suggest where to cut back (Gemma).

    python forecast.py

Reads data/transactions.csv and statements.csv; writes data/forecast.csv and forecast_tips.json.
Runs automatically when a fetch reads new statements; run it by hand after editing categories.
"""

import sys

from app import forecast


def main() -> int:
    forecast.update()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Re-extract text from already-unlocked statement PDFs. No Gmail, no passwords.

Use this after changing the extraction code, or to process PDFs downloaded earlier.
Prints only page counts, never statement contents.
To rebuild the transaction CSVs, use build_transactions.py.

    python extract_statements.py              # every account, skip statements already extracted
    python extract_statements.py --force      # redo all
    python extract_statements.py --account mom
"""

import argparse
import sys

from app.pdf_reader import extract, save_extraction
from app.storage import AccountDirs


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from unlocked statement PDFs.")
    parser.add_argument("--account", help="Only this account (default: all)")
    parser.add_argument("--force", action="store_true", help="Re-extract even if output already exists")
    args = parser.parse_args()

    accounts = [args.account] if args.account else AccountDirs.accounts()
    done = 0
    for account in accounts:
        dirs = AccountDirs.for_account(account)
        pdfs = sorted(dirs.unlocked.glob("*.pdf"))
        locked_only = len(list(dirs.pdf.glob("*.pdf"))) - len(pdfs)
        print(f"[{account}] {len(pdfs)} unlocked PDF(s)"
              + (f", {locked_only} not unlocked yet (run download_statements.py)" if locked_only > 0 else ""))
        for pdf in pdfs:
            text_path = dirs.text / f"{pdf.stem}.txt"
            if not args.force and text_path.exists():
                continue
            result = extract(pdf)
            save_extraction(result, text_path)
            done += 1
            print(f"  {pdf.name}: {result.pages} page(s)")

    print(f"Done. {done} extracted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

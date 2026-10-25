"""Download statement PDFs from Gmail, work out their passwords, and turn them into transactions.

Passwords are worked out from the email itself: a local model (Gemma via Ollama) reads the
bank's password instructions, and Python fills them in from each family member's details
(see setup_profile.py). Whichever person's details open the PDF is the cardholder.

Files land in data/<account>/ (gitignored): pdf/ as downloaded, unlocked/ decrypted copies,
text/ column-aligned text, meta/ whose card and how it was unlocked. New statements are then parsed
into data/transactions.csv (see build_transactions.py), and the forecast is updated (forecast.py).
Without a search it uses the statement subject in app/settings.py, and only looks at email since
the last fetch with that search (saved in data/<account>/sync.json). --newer-than looks further back. Emails already unlocked are
skipped, so it's safe to re-run. The dashboard's "Fetch new statements" button runs the same thing.

Examples:
    python download_statements.py                       # new statements since last time
    python download_statements.py --subject "Credit Card Statement"
    python download_statements.py --subject "Statement" --newer-than 90d
    python download_statements.py --account mom --from sbicard.com --no-llm
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

import build_transactions
from app import llm
from app import accounts, forecast, settings, sync
from app.csv_store import STATEMENTS, read_rows
from app.cli import DEFAULT_NEWER_THAN, add_search_args, base_query_from_args, has_time_filter
from app.gmail_client import (PROJECT_ROOT, download_pdf_attachments, email_body_text, get_message,
                              get_service, search_message_ids, summarize)
from app.password_hints import Recipe, asdicts, describe, find_hint, read_with_llm, read_with_rules
from app.passwords import COMMON_RECIPES, candidates, card_digits_in
from app.pdf_reader import extract, first_working, is_encrypted, save_extraction, save_unlocked, try_open
from app.profiles import load_profiles
from app.storage import AccountDirs, safe_name, write_private


class HintReader:
    """Reads password hints, caching by hint text so each bank's wording costs one model call per run."""

    def __init__(self, use_llm: bool):
        self.use_llm = use_llm
        self.cache: dict[str, list[tuple[str, Recipe]]] = {}

    def recipes(self, hint: str) -> list[tuple[str, Recipe]]:
        """Return (source, recipe) pairs: model answers first, then rule-based ones it missed."""
        if hint in self.cache:
            return self.cache[hint]
        found: list[tuple[str, Recipe]] = []
        if self.use_llm and hint:
            try:
                found += [(llm.MODEL, r) for r in read_with_llm(hint)]
            except llm.LLMUnavailable as e:
                print(f"   Model unavailable, using rules only: {e}")
                self.use_llm = False
        found += [("rules", r) for r in read_with_rules(hint) if r not in {r for _, r in found}]
        self.cache[hint] = found
        return found


def unlock(pdf_path: Path, email_text: str, profiles, hints: HintReader,
           interactive: bool) -> tuple[str, dict] | None:
    """Return (password, info) for the PDF, or None if it stays locked. info says who and how."""
    if not is_encrypted(pdf_path):
        print("   Not password-protected")
        return "", {"holder": None, "method": "not protected"}

    hint = find_hint(email_text)
    extra_cards = card_digits_in(email_text)
    tried: set[str] = set()

    for source, recipe in hints.recipes(hint):
        print(f"   Hint ({source}): {describe(recipe)}")
        options = [c for c in candidates([recipe], profiles, extra_cards) if c[1] not in tried]
        tried.update(pw for _, pw in options)
        if hit := first_working(pdf_path, options):
            print(f"   Unlocked with {hit[0]}'s details")
            return hit[1], {"holder": hit[0], "method": source, "rule": asdicts(recipe)}

    if not hint:
        print("   No password hint in the email")
    for recipe in COMMON_RECIPES:
        options = [c for c in candidates([recipe], profiles, extra_cards) if c[1] not in tried]
        tried.update(pw for _, pw in options)
        if hit := first_working(pdf_path, options):
            print(f"   Unlocked with {hit[0]}'s details (common pattern: {describe(recipe)})")
            return hit[1], {"holder": hit[0], "method": "common pattern", "rule": asdicts(recipe)}

    if not interactive:
        print("   Still locked: no rule matched anyone's details")
        return None
    while password := getpass.getpass("   Couldn't work it out. Password (Enter to skip): "):
        if try_open(pdf_path, password):
            return password, {"holder": None, "method": "typed"}
        print("   That didn't work, try again.")
    return None


def _read_pdfs() -> set[str]:
    """PDF file names whose transactions are already in statements.csv."""
    return {row["pdf"] for row in read_rows(STATEMENTS)}


def fetch(account: str, search: str, time_filter: str, max_results: int = 50, use_llm: bool = True,
          interactive: bool = False) -> dict:
    """Search one Gmail inbox for statement emails since the last fetch; download, unlock and read only
    the new ones into the CSVs. If there are none, nothing else happens.

    The inbox's last-fetch time is saved only once every statement found has been read, so an
    interrupted or failed fetch (Ollama down, a password not worked out yet) is picked up next time.
    Prints progress as it goes and returns counts. Raises FileNotFoundError if Gmail isn't set up.
    """
    counts = {"found": 0, "downloaded": 0, "extracted": 0, "locked": 0, "done before": 0, "to read": 0, "read": 0}
    profiles = load_profiles()
    if not profiles:
        print("No family details saved yet. Add people in Family first.")
        return counts

    started = sync.now()
    query = f"{search} {time_filter}".strip()
    print(f"[{account}] Connecting to Gmail...")
    service = get_service(account)  # may open a browser to sign in again (Google expires test tokens weekly)
    print(f"Query: {query}")

    dirs = AccountDirs.for_account(account)
    hints = HintReader(use_llm=use_llm)
    already_read = _read_pdfs()
    ids = search_message_ids(service, query, max_results)
    counts["found"] = len(ids)

    for message_id in ids:
        existing = sorted(dirs.pdf.glob(f"*_{message_id}_*.pdf"))
        if existing and all(p.name in already_read for p in existing):
            counts["done before"] += len(existing)
            continue
        if existing and all((dirs.unlocked / p.name).exists() for p in existing):
            counts["to read"] += sum(p.name not in already_read for p in existing)  # unlocked, not read yet
            continue

        msg = get_message(service, message_id)
        email = summarize(msg)
        email_text = f"{email.subject}\n{email_body_text(msg)}"
        print(f"{email.received_on}  {email.subject}")

        pdf_paths = existing
        if not pdf_paths:
            for filename, content in download_pdf_attachments(service, msg):
                path = dirs.pdf / f"{email.received_on}_{message_id}_{safe_name(filename)}"
                write_private(path, content)
                pdf_paths.append(path)
                counts["downloaded"] += 1
                print(f"   Saved {path.name}")

        for pdf_path in pdf_paths:
            unlocked_path = dirs.unlocked / pdf_path.name
            if unlocked_path.exists():
                counts["to read"] += pdf_path.name not in already_read
                continue
            result = unlock(pdf_path, email_text, profiles, hints, interactive)
            if result is None:
                counts["locked"] += 1
                continue
            password, info = result
            save_unlocked(pdf_path, unlocked_path, password)

            extraction = extract(unlocked_path)
            save_extraction(extraction, dirs.text / f"{pdf_path.stem}.txt")
            meta = {"gmail_id": message_id, "subject": email.subject, "sender": email.sender,
                    "received_on": email.received_on, "pdf": pdf_path.name, "pages": extraction.pages,
                    "cards_in_email": card_digits_in(email_text), **info}
            write_private(dirs.meta / f"{pdf_path.stem}.json", json.dumps(meta, indent=2))
            counts["extracted"] += 1
            print(f"   Unlocked ({extraction.pages} page(s))")
        print()

    new = counts["extracted"] + counts["to read"]
    if not new and not counts["locked"]:
        print("No new statements.")
        sync.save(account, started, search)
        return counts

    if new:
        print(f"Reading {new} new statement(s) with Gemma...\n")
        build_transactions.run(use_llm=use_llm, accounts=[account])

    read_now = _read_pdfs()
    counts["read"] = len(read_now - already_read)
    unread = [p for p in dirs.unlocked.glob("*.pdf") if p.name not in read_now]
    if unread or counts["locked"]:
        # Keep the old time so these are tried again on the next fetch.
        if unread:
            print(f"{len(unread)} statement(s) couldn't be read yet; the next fetch will try again.")
        if counts["locked"]:
            print(f"{counts['locked']} statement(s) still locked (add the person in Family?); "
                  "the next fetch will try again.")
    else:
        sync.save(account, started, search)
    print(f"Done. {counts['downloaded']} downloaded, {counts['extracted']} unlocked, {counts['read']} read.")
    return counts


def connected_accounts() -> list[str]:
    """Gmail inboxes that have signed in at least once."""
    return accounts.connected()


def main() -> int:
    parser = argparse.ArgumentParser(description="Download, unlock and extract statement PDFs from Gmail.")
    add_search_args(parser)
    parser.add_argument("--no-prompt", action="store_true",
                        help="Never ask for a password; leave PDFs locked if it can't be worked out")
    parser.add_argument("--no-llm", action="store_true", help="Read password hints with rules only")
    args = parser.parse_args()
    args.pdf = True  # this script only cares about emails with PDFs

    if args.query or args.subject or args.sender:
        search = base_query_from_args(parser, args)
    else:
        search = settings.gmail_search()

    # Time window: an explicit --newer-than wins, then a time filter inside --query, then "since the
    # last fetch", and only for a never-fetched account the default window.
    if args.newer_than:
        time_filter = f"newer_than:{args.newer_than}"
    elif has_time_filter(search):
        time_filter = ""
    else:
        time_filter = sync.since_filter(args.account, search) or f"newer_than:{DEFAULT_NEWER_THAN}"

    try:
        counts = fetch(args.account, search, time_filter, args.max, use_llm=not args.no_llm,
                       interactive=sys.stdin.isatty() and not args.no_prompt)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    if counts["read"]:  # new data, so the forecast changes too; nothing new leaves it as it is
        print()
        forecast.update()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Find Gmail messages by subject and list them.

Examples:
    python fetch_emails.py --subject "Credit Card Statement"
    python fetch_emails.py --subject "Statement" --pdf --newer-than 40d
    python fetch_emails.py --query 'from:(alerts@hdfcbank.net) has:attachment'
"""

import argparse
import sys

from app.cli import add_search_args, query_from_args
from app.gmail_client import get_email_summary, get_service, search_message_ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Search Gmail (read-only) for emails by subject.")
    add_search_args(parser)
    args = parser.parse_args()
    query = query_from_args(parser, args)

    try:
        service = get_service(args.account)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Query: {query}\n")

    ids = search_message_ids(service, query, args.max)
    if not ids:
        print("No matching emails.")
        return 0

    for i, message_id in enumerate(ids, 1):
        email = get_email_summary(service, message_id)
        print(f"{i}. {email.subject}")
        print(f"   From: {email.sender}")
        print(f"   Date: {email.date}")
        print(f"   ID:   {email.id}")
        if email.attachments:
            print(f"   Attachments: {', '.join(email.attachments)}")
        print()

    print(f"{len(ids)} email(s) found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

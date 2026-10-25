"""Settings you might want to change."""

# Which emails count as statements, by subject. Real bank emails say "Credit Card Statement";
# the demo kit's emails say "Demo Credit Card Statement". Change this line to fetch the demo emails.
STATEMENT_SUBJECT = "Demo Credit Card Statement"

DEMO_SUBJECT = "Demo Credit Card Statement"


def gmail_search() -> str:
    """The Gmail search used to find statement emails (the time window is added separately)."""
    search = f'subject:"{STATEMENT_SUBJECT}" has:attachment filename:pdf'
    if STATEMENT_SUBJECT != DEMO_SUBJECT:
        search += f' -subject:"{DEMO_SUBJECT}"'  # "Demo Credit Card Statement" also contains the real phrase
    return search

"""Shared command-line options for the Gmail scripts."""

import argparse

from app.gmail_client import build_query

DEFAULT_NEWER_THAN = "30d"  # how far back to look when there's nothing better to go on


def add_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subject", help="Text that must appear in the subject line")
    parser.add_argument("--from", dest="sender", help="Sender address or domain")
    parser.add_argument("--newer-than",
                        help=f"Gmail age filter, e.g. 40d, 6m, 1y (default {DEFAULT_NEWER_THAN})")
    parser.add_argument("--pdf", action="store_true", help="Only emails with a PDF attachment")
    parser.add_argument("--query", help="Raw Gmail search query (overrides the other filters)")
    parser.add_argument("--max", type=int, default=25, help="Maximum emails to return (default 25)")
    parser.add_argument("--account", default="default",
                        help="Name for this Gmail connection, e.g. me, mom, dad (default: default)")


def base_query_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """The search without any time filter."""
    if args.query:
        return args.query
    if not (args.subject or args.sender):
        parser.error("Give --subject, --from, or --query")
    return build_query(args.subject, args.sender, None, args.pdf)


def has_time_filter(query: str) -> bool:
    return any(term in query for term in ("newer_than:", "after:", "older_than:", "before:"))


def query_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    query = base_query_from_args(parser, args)
    if args.newer_than or not has_time_filter(query):
        query += f" newer_than:{args.newer_than or DEFAULT_NEWER_THAN}"
    return query

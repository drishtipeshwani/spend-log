"""Connected Gmail inboxes. Each has a short name (default, mom, dad) and a token in secrets/.

secrets/accounts.json remembers which address each one is, so re-signing in after Google's weekly
expiry pre-selects the right Google account, and the same inbox can't be connected twice.
"""

from __future__ import annotations

import json
import os

from app.gmail_client import SECRETS_DIR, get_service, token_path

ACCOUNTS_FILE = SECRETS_DIR / "accounts.json"


def _load() -> dict[str, str]:
    return json.loads(ACCOUNTS_FILE.read_text()) if ACCOUNTS_FILE.exists() else {}


def _save(data: dict[str, str]) -> None:
    SECRETS_DIR.mkdir(exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(ACCOUNTS_FILE, 0o600)


def connected() -> list[str]:
    """Names of inboxes that have signed in (secrets/token_<name>.json)."""
    return sorted(p.stem.removeprefix("token_") for p in SECRETS_DIR.glob("token_*.json"))


def address(account: str) -> str:
    """The Gmail address of a connected inbox (asked from Gmail once, then remembered)."""
    known = _load()
    if account not in known and token_path(account).exists():
        profile = get_service(account).users().getProfile(userId="me").execute()
        known[account] = profile["emailAddress"]
        _save(known)
    return known.get(account, "")


def login_hint(account: str) -> str:
    return _load().get(account, "")


def connect(account: str, email: str = "") -> str:
    """Sign in to Gmail (opens the browser) and save the token as `account`. Returns the address.

    Raises ValueError if that inbox is already connected under another name.
    """
    get_service(account, login_hint=email)
    profile = get_service(account).users().getProfile(userId="me").execute()
    signed_in = profile["emailAddress"]
    known = _load()
    other = next((name for name, addr in known.items() if addr.lower() == signed_in.lower() and name != account), None)
    if other:
        token_path(account).unlink(missing_ok=True)
        raise ValueError(f"{signed_in} is already connected as '{other}'.")
    known[account] = signed_in
    _save(known)
    return signed_in


def disconnect(account: str) -> None:
    token_path(account).unlink(missing_ok=True)
    known = _load()
    known.pop(account, None)
    _save(known)

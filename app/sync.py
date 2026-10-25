"""Remember when each Gmail account was last fetched, so the next fetch only looks at newer email.

Stored in data/<account>/sync.json, one time per Gmail search, so switching between the real and
demo subjects (app/settings.py) never skips emails.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from app.storage import AccountDirs, DATA_DIR, safe_name, write_private

OVERLAP = timedelta(0)  # search exactly from the last fetch; emails already read are skipped anyway


def _path(account: str):
    return DATA_DIR / safe_name(account) / "sync.json"


def _load(account: str) -> dict:
    path = _path(account)
    return json.loads(path.read_text()) if path.exists() else {}


def save(account: str, fetched_at: datetime, search: str) -> None:
    data = _load(account)
    times = data.get("last_fetch_by_search", {})
    times[search] = fetched_at.isoformat()
    AccountDirs.for_account(account)  # makes the folder
    write_private(_path(account), json.dumps({"last_fetch_by_search": times}, indent=2))


def last_fetch(account: str, search: str) -> datetime | None:
    value = _load(account).get("last_fetch_by_search", {}).get(search)
    return datetime.fromisoformat(value) if value else None


def latest_statement_date(account: str) -> date | None:
    """When the newest statement already on disk arrived, for searches that haven't been fetched yet."""
    dirs = AccountDirs.for_account(account)
    dates = [json.loads(p.read_text()).get("received_on") for p in dirs.meta.glob("*.json")]
    dates = [d for d in dates if d]
    return date.fromisoformat(max(dates)) if dates else None


def since_filter(account: str, search: str) -> str | None:
    """Gmail search term for "since the last fetch with this search", or None for a brand-new account."""
    if last := last_fetch(account, search):
        return f"after:{int((last - OVERLAP).timestamp())}"
    if newest := latest_statement_date(account):
        return f"after:{(newest - OVERLAP).strftime('%Y/%m/%d')}"
    return None


def now() -> datetime:
    return datetime.now(timezone.utc)

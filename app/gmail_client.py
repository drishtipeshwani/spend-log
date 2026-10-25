"""Read-only Gmail access: OAuth on the local machine, then search messages and fetch PDF attachments.

The OAuth client file and the saved token both live in ./secrets, which is gitignored.
Nothing here logs email bodies.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = PROJECT_ROOT / "secrets"
CREDENTIALS_FILE = SECRETS_DIR / "credentials.json"


@dataclass
class EmailSummary:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    received_on: str  # YYYY-MM-DD, from Gmail's internal timestamp
    attachments: list[str] = field(default_factory=list)


def token_path(account: str) -> Path:
    """One token file per connected account, so each family member can connect separately."""
    return SECRETS_DIR / f"token_{account}.json"


def _remembered_address(account: str) -> str:
    """The address this account signed in with before (see app/accounts.py)."""
    known = SECRETS_DIR / "accounts.json"
    return json.loads(known.read_text(encoding="utf-8")).get(account, "") if known.exists() else ""


def get_credentials(account: str = "default", login_hint: str = "") -> Credentials:
    """Saved credentials for an account, refreshed if needed. Signs in through the browser when there
    are none; `login_hint` (an email address) pre-selects that Google account on the sign-in page."""
    token_file = token_path(account)
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            # In Google's "testing" mode refresh tokens expire after 7 days; fall back to a fresh login.
            creds = None

    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"OAuth client file not found at {CREDENTIALS_FILE}. "
                "Download it from Google Cloud Console (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        hint = login_hint or _remembered_address(account)
        extra = {"login_hint": hint} if hint else {}
        # Give up after 5 minutes rather than wait forever (e.g. if Google blocked the sign-in because the
        # address isn't a test user of the Cloud project).
        creds = flow.run_local_server(port=0, prompt="consent", timeout_seconds=300, **extra)
        if not creds or not creds.token:
            raise TimeoutError("Google sign-in didn't finish.")

    SECRETS_DIR.mkdir(exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_file, 0o600)
    return creds


def get_service(account: str = "default", login_hint: str = ""):
    return build("gmail", "v1", credentials=get_credentials(account, login_hint), cache_discovery=False)


def build_query(subject: str | None = None, sender: str | None = None,
                newer_than: str | None = None, has_pdf: bool = False) -> str:
    parts = []
    if subject:
        parts.append(f'subject:"{subject}"')
    if sender:
        parts.append(f"from:({sender})")
    if has_pdf:
        parts.append("has:attachment filename:pdf")
    if newer_than:
        parts.append(f"newer_than:{newer_than}")
    return " ".join(parts)


def search_message_ids(service, query: str, max_results: int = 25) -> list[str]:
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(100, max_results - len(ids)),
            pageToken=page_token,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _attachment_parts(payload: dict) -> list[dict]:
    parts = []
    stack = [payload]
    while stack:
        part = stack.pop()
        if part.get("filename"):
            parts.append(part)
        stack.extend(reversed(part.get("parts", [])))
    return parts


def get_message(service, message_id: str) -> dict:
    return service.users().messages().get(userId="me", id=message_id, format="full").execute()


def summarize(msg: dict) -> EmailSummary:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    return EmailSummary(
        id=msg["id"],
        thread_id=msg["threadId"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        date=headers.get("date", ""),
        received_on=received.date().isoformat(),
        attachments=[p["filename"] for p in _attachment_parts(payload)],
    )


def get_email_summary(service, message_id: str) -> EmailSummary:
    return summarize(get_message(service, message_id))


class _HTMLText(HTMLParser):
    BLOCK_TAGS = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "td"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def email_body_text(msg: dict) -> str:
    """Plain text of the email body (HTML is stripped). Used for reading password hints; never logged."""
    plain, html = [], []
    stack = [msg.get("payload", {})]
    while stack:
        part = stack.pop()
        stack.extend(reversed(part.get("parts", [])))
        data = part.get("body", {}).get("data")
        if part.get("filename") or not data:
            continue
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if part.get("mimeType") == "text/plain":
            plain.append(text)
        elif part.get("mimeType") == "text/html":
            html.append(text)

    if plain:
        return "\n".join(plain)
    parser = _HTMLText()
    parser.feed("\n".join(html))
    lines = (" ".join(line.split()) for line in "".join(parser.chunks).splitlines())
    return "\n".join(line for line in lines if line)


def download_pdf_attachments(service, msg: dict) -> list[tuple[str, bytes]]:
    """Return (filename, bytes) for every PDF attached to an already-fetched message."""
    pdfs = []
    for part in _attachment_parts(msg.get("payload", {})):
        name = part["filename"]
        if not (name.lower().endswith(".pdf") or part.get("mimeType") == "application/pdf"):
            continue
        body = part.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            data = service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]
            ).execute()["data"]
        if data:
            pdfs.append((name, base64.urlsafe_b64decode(data)))
    return pdfs

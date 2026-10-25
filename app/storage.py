"""Where downloaded and extracted statement files live. Everything is under data/ (gitignored).

Set SPEND_LOG_DATA to point at another folder (the tests use this to work in a throwaway folder).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.gmail_client import PROJECT_ROOT

DATA_DIR = Path(os.environ.get("SPEND_LOG_DATA") or PROJECT_ROOT / "data").resolve()


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def write_private(path: Path, data: str | bytes) -> None:
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="")  # UTF-8 everywhere (₹), no \r\n doubling on Windows
    os.chmod(path, 0o600)


@dataclass
class AccountDirs:
    pdf: Path       # PDFs exactly as downloaded (usually password-protected)
    unlocked: Path  # decrypted copies, so extraction can be re-run without passwords
    text: Path      # layout-preserving text, one .txt per statement
    meta: Path      # whose card, how the password was found

    @classmethod
    def for_account(cls, account: str) -> AccountDirs:
        root = DATA_DIR / safe_name(account)
        dirs = cls(*(root / name for name in ("pdf", "unlocked", "text", "meta")))
        for d in vars(dirs).values():
            d.mkdir(parents=True, exist_ok=True)
        return dirs

    @staticmethod
    def accounts() -> list[str]:
        return sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir()) if DATA_DIR.exists() else []

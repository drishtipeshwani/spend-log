"""Unlock password-protected statement PDFs and extract their text.

Passwords are never printed or logged.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pikepdf
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from app.storage import write_private

def try_open(path: Path, password: str) -> bool:
    try:
        with pdfplumber.open(path, password=password):
            return True
    except PdfminerException as e:
        # pdfplumber wraps pdfminer's error; anything other than a bad password is a real failure.
        if e.args and isinstance(e.args[0], PDFPasswordIncorrect):
            return False
        raise


def is_encrypted(path: Path) -> bool:
    return not try_open(path, "")


def first_working(path: Path, candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Return the first (label, password) that opens the PDF, or None."""
    for label, password in candidates:
        if try_open(path, password):
            return label, password
    return None


def save_unlocked(src: Path, dst: Path, password: str) -> None:
    """Write a decrypted copy of the PDF (file mode 600)."""
    if password:
        with pikepdf.open(src, password=password) as pdf:
            pdf.save(dst)  # saving without an encryption argument drops the password
    else:
        shutil.copyfile(src, dst)
    dst.chmod(0o600)


# --- Extraction ----------------------------------------------------------------------------

@dataclass
class Extraction:
    text: str
    pages: int


def layout_text(page) -> str:
    """Page text with columns kept aligned by spaces, trailing whitespace and blank runs trimmed."""
    lines = [line.rstrip() for line in (page.extract_text(layout=True) or "").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def extract(path: Path, password: str = "") -> Extraction:
    with pdfplumber.open(path, password=password) as pdf:
        texts = [f"--- page {i} ---\n{layout_text(page)}" for i, page in enumerate(pdf.pages, 1)]
        return Extraction(text="\n\n".join(texts), pages=len(pdf.pages))


def save_extraction(result: Extraction, text_path: Path) -> None:
    write_private(text_path, result.text)

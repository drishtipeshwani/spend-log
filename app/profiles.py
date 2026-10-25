"""Family member details used to work out statement passwords.

Stored in secrets/profiles.json (gitignored, file mode 600). These values never go to the
model: the model only describes the password rule, and Python fills in the details.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date

from app.gmail_client import SECRETS_DIR

PROFILES_FILE = SECRETS_DIR / "profiles.json"


@dataclass
class Profile:
    label: str          # me, mom, dad
    full_name: str      # as printed on the card
    dob: str            # YYYY-MM-DD
    mobile: str = ""
    card_last4: list[str] = field(default_factory=list)
    email: str = ""     # their Gmail, if their statements arrive in their own inbox

    @property
    def birth_date(self) -> date:
        return date.fromisoformat(self.dob)


def load_profiles() -> list[Profile]:
    if not PROFILES_FILE.exists():
        return []
    return [Profile(**p) for p in json.loads(PROFILES_FILE.read_text())]


def save_profiles(profiles: list[Profile]) -> None:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps([asdict(p) for p in profiles], indent=2))
    os.chmod(PROFILES_FILE, 0o600)

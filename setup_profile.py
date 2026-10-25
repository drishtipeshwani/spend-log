"""Add or update a family member's details, used to work out statement PDF passwords.

    python setup_profile.py          # add or update a person
    python setup_profile.py --list   # show who is set up (names only)
"""

import argparse
import re
import sys
from datetime import datetime

from app.profiles import Profile, load_profiles, save_profiles


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def ask_dob(default: str = "") -> str:
    shown = datetime.strptime(default, "%Y-%m-%d").strftime("%d-%m-%Y") if default else ""
    while True:
        raw = ask("Date of birth (DD-MM-YYYY)", shown)
        try:
            return datetime.strptime(raw.replace("/", "-"), "%d-%m-%Y").date().isoformat()
        except ValueError:
            print("  Please use DD-MM-YYYY, e.g. 09-12-1995")


def main() -> int:
    parser = argparse.ArgumentParser(description="Add or update a family member's details.")
    parser.add_argument("--list", action="store_true", help="List saved people")
    args = parser.parse_args()

    profiles = load_profiles()
    if args.list:
        for p in profiles:
            print(f"{p.label}: {p.full_name}")
        if not profiles:
            print("No one set up yet.")
        return 0

    label = ask("Short label (e.g. me, mom, dad)").lower()
    if not label:
        print("A label is required.")
        return 1
    existing = next((p for p in profiles if p.label == label), None)

    full_name = ask("Full name as printed on the card", existing.full_name if existing else "")
    dob = ask_dob(existing.dob if existing else "")
    mobile = re.sub(r"\D", "", ask("Mobile number (optional)", existing.mobile if existing else ""))
    email = ask("Gmail address their statements arrive at (optional)", existing.email if existing else "")
    cards_raw = ask("Last 4 digits of their cards, comma-separated (optional)",
                    ",".join(existing.card_last4) if existing else "")
    card_last4 = [c for c in re.findall(r"\d{4}", cards_raw)]

    profile = Profile(label=label, full_name=full_name, dob=dob, mobile=mobile, card_last4=card_last4,
                      email=email)
    profiles = [p for p in profiles if p.label != label] + [profile]
    save_profiles(profiles)
    print(f"Saved '{label}'. Set up so far: {', '.join(p.label for p in profiles)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

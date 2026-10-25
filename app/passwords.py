"""Turn password recipes plus family profiles into candidate passwords.

When a recipe leaves something open (case not stated, no date layout given), every reasonable
variant is generated. The number of candidates stays small, and trying one is a local file open.
"""

from __future__ import annotations

import itertools
import re
from datetime import date

from app.password_hints import Part, Recipe
from app.profiles import Profile

_DATE_FORMATS = {
    "DDMM": "%d%m",
    "DDMMYY": "%d%m%y",
    "DDMMYYYY": "%d%m%Y",
    "MMDD": "%m%d",
    "YYYY": "%Y",
    "YYYYMMDD": "%Y%m%d",
}
_UNSPECIFIED_DATES = ["DDMM", "DDMMYYYY", "DDMMYY"]

# Patterns Indian card issuers commonly use, tried when the email gives no usable hint.
COMMON_RECIPES: list[Recipe] = [
    (Part("name", count=4), Part("dob", date_format="DDMM")),
    (Part("name", count=4), Part("dob", date_format="DDMMYYYY")),
    (Part("dob", date_format="DDMMYYYY"),),
    (Part("dob", date_format="DDMMYYYY"), Part("card", take="last", count=4)),
    (Part("dob", date_format="DDMM"), Part("card", take="last", count=4)),
    (Part("name", count=4), Part("card", take="last", count=4)),
    (Part("name", count=4), Part("mobile", take="last", count=4)),
]


def card_digits_in(text: str) -> list[str]:
    """Last-4 card digits mentioned in an email, e.g. 'card ending 1234' or 'XXXX XXXX XXXX 1234'."""
    found = re.findall(r"(?:ending(?:\s+(?:in|with))?|[xX*]{4}[\s-]?)\s*(\d{4})\b", text)
    return list(dict.fromkeys(found))


def _slice(value: str, part: Part) -> str:
    if not part.count:
        return value
    return value[: part.count] if part.take == "first" else value[-part.count:]


def _part_values(part: Part, profile: Profile, extra_cards: list[str]) -> list[str]:
    if part.field == "text":
        return [part.text]

    if part.field == "name":
        words = re.findall(r"[A-Za-z]+", profile.full_name)
        if not words:
            return []
        bases = {_slice(words[0], part), _slice("".join(words), part)}
        cases = {"upper": [str.upper], "lower": [str.lower]}.get(part.case, [str.upper, str.lower, str.capitalize])
        return list(dict.fromkeys(c(b) for b in sorted(bases) for c in cases))

    if part.field == "dob":
        formats = [part.date_format] if part.date_format in _DATE_FORMATS else _UNSPECIFIED_DATES
        born: date = profile.birth_date
        return [born.strftime(_DATE_FORMATS[f]) for f in formats]

    if part.field == "card":
        cards = list(dict.fromkeys(profile.card_last4 + extra_cards))
        if part.take == "last" and part.count <= 4:
            return [_slice(c, part) for c in cards]
        return []  # we only ever know the last 4 digits

    if part.field == "mobile":
        return [_slice(profile.mobile, part)] if profile.mobile else []

    return []


def candidates(recipes: list[Recipe], profiles: list[Profile],
               extra_cards: list[str] | None = None) -> list[tuple[str, str]]:
    """Return unique (profile label, password) pairs, in the order they should be tried."""
    out: dict[str, str] = {}
    for recipe in recipes:
        for profile in profiles:
            values = [_part_values(p, profile, extra_cards or []) for p in recipe]
            if not all(values):
                continue
            for combo in itertools.product(*values):
                out.setdefault("".join(combo), profile.label)
    return [(label, pw) for pw, label in out.items()]

"""Read a statement email's password instructions and turn them into a password recipe.

A recipe is a list of parts, e.g. "first 4 letters of name in capitals" + "date of birth as DDMM".
It describes the shape of the password only. Real values come from app.profiles later, so the
model never sees anyone's name or date of birth.

Two readers produce recipes:
  - read_with_llm: a local open model (Gemma via Ollama) with a fixed JSON schema
  - read_with_rules: regexes for the common phrasings, used as a fallback and cross-check
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app import llm

FIELDS = ["name", "dob", "card", "mobile", "text"]
DATE_FORMATS = ["DDMM", "DDMMYY", "DDMMYYYY", "MMDD", "YYYY", "YYYYMMDD", "unspecified"]
CASES = ["upper", "lower", "unspecified"]


@dataclass(frozen=True)
class Part:
    field: str                       # one of FIELDS
    take: str = "first"              # "first" or "last" characters (name, card, mobile)
    count: int = 0                   # how many characters; 0 means all
    date_format: str = "unspecified" # dob only
    case: str = "unspecified"        # name only
    text: str = ""                   # literal text, field == "text"

    def describe(self) -> str:
        if self.field == "name":
            n = f"{self.take} {self.count} letters of name" if self.count else "full name"
            return n + ("" if self.case == "unspecified" else f" ({self.case})")
        if self.field == "dob":
            return f"DOB as {self.date_format}" if self.date_format != "unspecified" else "DOB"
        if self.field in ("card", "mobile"):
            return f"{self.take} {self.count} digits of {self.field}"
        return f'"{self.text}"'


Recipe = tuple[Part, ...]


def describe(recipe: Recipe) -> str:
    return " + ".join(p.describe() for p in recipe)


# --- Finding the hint in the email ---------------------------------------------------------

_HINT_LINE = re.compile(r"password|passcode|pwd|protected|to open (the|your) (statement|attachment)", re.I)


def find_hint(body: str, max_chars: int = 1500) -> str:
    """The lines around any mention of a password; empty if the email has none."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if _HINT_LINE.search(line):
            keep.update(range(i, min(i + 3, len(lines))))  # examples often follow on the next lines
    return "\n".join(lines[i] for i in sorted(keep))[:max_chars]


# --- Local model ---------------------------------------------------------------------------

_PART_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string", "enum": FIELDS},
        "take": {"type": "string", "enum": ["first", "last"]},
        "count": {"type": "integer"},
        "date_format": {"type": "string", "enum": DATE_FORMATS},
        "case": {"type": "string", "enum": CASES},
        "text": {"type": "string"},
    },
    "required": ["field"],
}
_SCHEMA = {
    "type": "object",
    "properties": {
        "recipes": {
            "type": "array",
            "items": {"type": "object", "properties": {"parts": {"type": "array", "items": _PART_SCHEMA}},
                      "required": ["parts"]},
        }
    },
    "required": ["recipes"],
}

_SYSTEM = """You read the password instructions from Indian bank credit card statement emails and \
describe how the PDF password is built, as JSON.

Each recipe is an ordered list of parts joined together with no separator. Part types:
- {"field": "name", "take": "first"|"last", "count": N, "case": "upper"|"lower"|"unspecified"}: \
letters of the cardholder's name. case "upper" for capital/upper case/caps/block letters, "lower" \
for small/lower case, otherwise "unspecified".
- {"field": "dob", "date_format": "DDMM"|"DDMMYY"|"DDMMYYYY"|"MMDD"|"YYYY"|"YYYYMMDD"|"unspecified"}: \
date of birth. "date and month" means DDMM; "year of birth" means YYYY.
- {"field": "card", "take": "last", "count": N}: digits of the card number.
- {"field": "mobile", "take": "last", "count": N}: digits of the registered mobile number.
- {"field": "text", "text": "..."}: fixed literal text.

Include ONLY the parts the email explicitly says are in the password, in the order it says. Most \
passwords have one or two parts. Do not add a part just because a field type exists.
If the email gives alternatives (different rules for different customers), return one recipe per \
alternative. If there are no password instructions, return {"recipes": []}.
Never invent personal values. Ignore worked examples' values and warnings like "never share your password"."""

_EXAMPLES = [
    ("The password is the first four letters of your name in CAPITAL letters followed by your date "
     "of birth in DDMM format. E.g. for Rahul Sharma born 01/09/1990, password is RAHU0109.",
     '{"recipes": [{"parts": [{"field": "name", "take": "first", "count": 4, "case": "upper"}, '
     '{"field": "dob", "date_format": "DDMM"}]}]}'),
    ("To open the attachment, enter your date of birth in DDMMYYYY format.",
     '{"recipes": [{"parts": [{"field": "dob", "date_format": "DDMMYYYY"}]}]}'),
    ("Customers with a registered mobile: last 4 digits of the mobile number followed by the first "
     "4 letters of your name in lower case. All other customers: your date of birth as DDMMYY.",
     '{"recipes": [{"parts": [{"field": "mobile", "take": "last", "count": 4}, '
     '{"field": "name", "take": "first", "count": 4, "case": "lower"}]}, '
     '{"parts": [{"field": "dob", "date_format": "DDMMYY"}]}]}'),
]

# A part is only kept if the hint mentions its topic; this stops the model padding recipes.
_MENTIONS = {
    "name": re.compile(r"name", re.I),
    "dob": re.compile(r"birth|\bdob\b|\bd{2}[\s/.-]?m{2}|\byyyy\b", re.I),
    "card": re.compile(r"card", re.I),
    "mobile": re.compile(r"mobile|phone", re.I),
    "text": re.compile(r"."),
}


def read_with_llm(hint: str) -> list[Recipe]:
    """Raises llm.LLMUnavailable if Ollama isn't running."""
    if not hint:
        return []
    shots = "\n\n".join(f"Email:\n{q}\nAnswer:\n{a}" for q, a in _EXAMPLES)
    reply = llm.chat_json(_SYSTEM, f"{shots}\n\nEmail:\n{hint}\nAnswer:", _SCHEMA)
    recipes = []
    for r in reply.get("recipes", []):
        parts = [_clean_part(p) for p in r.get("parts", [])]
        parts = tuple(p for p in parts if p and _MENTIONS[p.field].search(hint))
        if parts and parts not in recipes:
            recipes.append(parts)
    return recipes


def _clean_part(raw: dict) -> Part | None:
    """Validate model output so a bad answer can't produce nonsense parts."""
    f = raw.get("field")
    if f not in FIELDS:
        return None
    if f == "text":
        return Part("text", text=str(raw.get("text", ""))) if raw.get("text") else None
    take = raw.get("take") if raw.get("take") in ("first", "last") else ("last" if f in ("card", "mobile") else "first")
    count = raw.get("count") if isinstance(raw.get("count"), int) and 0 <= raw["count"] <= 20 else 0
    if f in ("card", "mobile") and count == 0:
        count = 4
    return Part(
        field=f,
        take=take,
        count=count if f != "dob" else 0,
        date_format=raw.get("date_format") if f == "dob" and raw.get("date_format") in DATE_FORMATS else "unspecified",
        case=raw.get("case") if f == "name" and raw.get("case") in CASES else "unspecified",
    )


# --- Rules ---------------------------------------------------------------------------------

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_NUM = r"(\d+|one|two|three|four|five|six|seven|eight)"

_NAME = re.compile(
    rf"\b(first|last)\s+{_NUM}\s+(?:letters?|characters?|alphabets?)\s+of\s+(?:your|the)?\s*"
    r"(?:card\s*holder'?s?\s+|customer'?s?\s+|primary\s+)?(?:first\s+)?name", re.I)
_DIGITS = re.compile(
    rf"\b(last|first)\s+{_NUM}\s+digits\s+of\s+(?:your|the)?\s*(?:registered\s+)?(?:[\w-]+\s+){{0,2}}?"
    r"(card|mobile)", re.I)
_DOB_PHRASE = re.compile(r"date\s+of\s+birth|\bdob\b|birth\s*date", re.I)
_DATE_AND_MONTH = re.compile(r"(?:date|day)\s+and\s+month", re.I)
_DATE_TOKEN = re.compile(r"\b(dd[\s/.-]?mm(?:[\s/.-]?(?:yyyy|yy))?|mm[\s/.-]?dd|yyyy[\s/.-]?mm[\s/.-]?dd|yyyy)\b", re.I)
_BIRTH_YEAR = re.compile(r"birth\s+year|year\s+of\s+birth", re.I)
_UPPER = re.compile(r"capital|upper\s*case|\bcaps\b|block\s+letters", re.I)
_LOWER = re.compile(r"small\s+letters|lower\s*case", re.I)


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _WORDS[s.lower()]


def read_with_rules(hint: str) -> list[Recipe]:
    """One recipe from the whole hint, plus one per sentence when sentences give separate rules."""
    recipes = []
    whole = _rules_recipe(hint)
    if whole:
        recipes.append(whole)
    sentences = [s for s in re.split(r"(?<=[.;])\s+|\n", hint) if s.strip()]
    per_sentence = [r for r in map(_rules_recipe, sentences) if r]
    if len(per_sentence) > 1:
        recipes += [r for r in per_sentence if r not in recipes]
    return recipes


def _rules_recipe(hint: str) -> Recipe | None:
    found: list[tuple[int, Part]] = []

    for m in _NAME.finditer(hint):
        nearby = hint[max(0, m.start() - 60): m.end() + 80]
        case = "upper" if _UPPER.search(nearby) else "lower" if _LOWER.search(nearby) else "unspecified"
        found.append((m.start(), Part("name", take=m.group(1).lower(), count=_num(m.group(2)), case=case)))

    for m in _DIGITS.finditer(hint):
        found.append((m.start(), Part(m.group(3).lower(), take=m.group(1).lower(), count=_num(m.group(2)))))

    dob = _DOB_PHRASE.search(hint) or _BIRTH_YEAR.search(hint)
    token = _DATE_TOKEN.search(hint)
    if dob or token:
        if token:
            fmt = re.sub(r"[\s/.-]", "", token.group(1)).upper()
        elif _BIRTH_YEAR.search(hint):
            fmt = "YYYY"
        else:
            fmt = "DDMM" if _DATE_AND_MONTH.search(hint) else "unspecified"
        pos = min(m.start() for m in (dob, token) if m)
        if fmt in DATE_FORMATS:
            found.append((pos, Part("dob", date_format=fmt)))

    # One part per field: the first mention wins (later mentions are usually the worked example).
    seen, parts = set(), []
    for _, part in sorted(found, key=lambda x: x[0]):
        if part.field not in seen:
            seen.add(part.field)
            parts.append(part)
    return tuple(parts) or None


def asdicts(recipe: Recipe) -> list[dict]:
    return [asdict(p) for p in recipe]

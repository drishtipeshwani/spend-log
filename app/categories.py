"""Merchant names and spending categories.

Python groups raw descriptions into merchant keys ("UPI/ZOMATO IND" -> "ZOMATO"). Gemma then names
and categorizes each new key once, and the answer is cached in merchants.csv. Edit a category there
(and set category_source to "user") and it applies to every past and future transaction.
"""

from __future__ import annotations

import re

from app import llm

CATEGORIES = [
    "Food delivery", "Cafes & restaurants", "Groceries", "Shopping", "Travel", "Cabs & transport",
    "Fuel", "Vehicle", "Bills & utilities", "Subscriptions", "Entertainment", "Health", "Insurance", "Education",
    "Fees & charges", "Transfers", "Card payment", "Refund", "Other",
]
NOT_SPEND = {"Card payment"}  # money paid to the card, not money spent

_PREFIXES = re.compile(r"^(UPI|POS|ECOM|NEFT|IMPS|ACH)[/\s-]+", re.I)
_NOISE = {"IND", "INDIA", "IN", "PVT", "PRIVATE", "PRIVA", "LTD", "LIMITED", "LLP", "CO", "THE"}
_CARD_PAYMENT = re.compile(r"\b(PYMNT|PAYMENT)\b.*\b(RCV|RCVD|RECEIVED)\b|\bBBPS\b|THANK YOU", re.I)


def merchant_key(description: str) -> str:
    """Group key for a raw description: no rail prefix, legal suffixes, or reference numbers."""
    words = re.findall(r"[A-Za-z0-9&']+", _PREFIXES.sub("", description.upper()))
    kept = [w for w in words if w not in _NOISE and not w.isdigit()]
    return " ".join(kept) or description.upper().strip()


def same_brand(name: str, key: str) -> bool:
    """Could `name` be the brand behind merchant key `key`? True when a word of the key is spelled
    inside the name, in order ("AMZN" in "Amazon", "METRO" in "Namma Metro"). Guards against the model
    reusing a different known brand, e.g. calling ZOMATO "Swiggy"."""
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    def inside(word: str) -> bool:
        letters = iter(flat)
        return all(ch in letters for ch in word)
    return any(len(w) >= 3 and inside(w) for w in re.findall(r"[a-z0-9]+", key.lower()))


def rule_category(description: str, direction: str) -> tuple[str, str] | None:
    """(merchant, category) for cases that need no model."""
    if direction == "credit" and _CARD_PAYMENT.search(description):
        return "Card payment", "Card payment"
    return None


_SCHEMA = {
    "type": "object",
    "properties": {"merchants": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "merchant": {"type": "string"},
                       "category": {"type": "string", "enum": CATEGORIES}},
        "required": ["id", "merchant", "category"]}}},
    "required": ["merchants"],
}

_SYSTEM = f"""You categorize Indian credit card transactions. For each numbered line, give the \
merchant's brand name and one category from this list: {", ".join(CATEGORIES)}.

Brand name: the parent brand people know, short, written the way the brand writes it (BigBasket, \
DMart, IRCTC, LIC, BESCOM, IndiGo, YouTube Premium); otherwise Title Case. Drop words like Marketplace, Now, \
Media, Payments, Retail, Online. Examples: ZEPTONOW -> Zepto, ZEPTO MARKETPLACE -> Zepto, ZOMATO MEDIA \
-> Zomato, HDFCERGOGIC -> HDFC Ergo, AMAZON PAY INDIA -> Amazon, GOOGLE*YOUTUBEPREMIUM -> YouTube Premium. Keep separate services separate \
(Swiggy Instamart is not Swiggy). If a known merchant name is given, reuse it exactly for the same brand.

Guidance: Zomato, Swiggy = Food delivery. Zepto, Blinkit, BigBasket, Swiggy Instamart, DMart = \
Groceries. Amazon, Flipkart, Myntra = Shopping. Uber, Ola, Rapido, metro = Cabs & transport. Petrol pumps = Fuel. Car or bike service, \
repairs, tyres, FASTag tolls, parking = Vehicle. Restaurants and dine-in places = Cafes & restaurants, including ones whose names end in \
Foods, Kitchen, Biryani or Bhavan (e.g. Meghana Foods, Empire, Truffles, Mainland China). \
Airlines, IRCTC, hotels, ixigo, MakeMyTrip = Travel. Recurring digital plans (Netflix, Spotify, \
YouTube Premium, Amazon Prime, Disney+ Hotstar, JioCinema, Apple, iCloud, Google One, ChatGPT, \
Microsoft 365, Audible, Kindle Unlimited, gym memberships) = Subscriptions. One-off fun (BookMyShow, \
PVR, INOX, concerts, gaming top-ups) = Entertainment. Electricity, water, broadband, piped gas and LPG cylinders (Indane, HP Gas, Bharat \
Gas), and mobile recharges or postpaid bills (Jio, Airtel, Vi, BSNL) = Bills & utilities. Pharmacies, hospitals = \
Health. HDFC Ergo, ICICI Lombard, LIC, Star Health, policy premiums = Insurance. A credit that isn't \
a card payment is usually a Refund. A UPI payment to a person's name is \
Transfers. If unsure, use Other."""


def categorize(items: list[tuple[str, str, str]], known: list[str] | None = None,
               batch_size: int = 30) -> dict[str, tuple[str, str]]:
    """items: (key, example description, direction). Returns key -> (merchant, category).

    `known` merchant names are shown to the model so the same brand keeps one name.
    Raises llm.LLMUnavailable if Ollama isn't running. Keys the model skips are left out.
    """
    out: dict[str, tuple[str, str]] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        for _attempt in range(2):  # one retry for anything the model skipped
            todo = [(i, item) for i, item in enumerate(batch) if item[0] not in out]
            if not todo:
                break
            names = sorted(set(known or []) | {m for m, _ in out.values()})
            header = f"Known merchant names: {', '.join(names)}\n\n" if names else ""
            lines = "\n".join(f"{i}. {desc} ({direction})" for i, (_, desc, direction) in todo)
            reply = llm.chat_json(_SYSTEM, header + lines, _SCHEMA)
            valid_ids = {i for i, _ in todo}
            for m in reply.get("merchants", []):
                if m.get("id") in valid_ids and m.get("category") in CATEGORIES and m.get("merchant"):
                    out[batch[m["id"]][0]] = (m["merchant"].strip(), m["category"])
    return out


def apply_categories(txns: list[dict], merchants: dict[str, dict]) -> None:
    """Fill merchant/category on transaction rows (in place) from merchants.csv entries."""
    for t in txns:
        m = merchants.get(merchant_key(t["description"]))
        t["merchant"] = m["merchant"] if m else ""
        t["category"] = m["category"] if m else ""
        t["category_source"] = m["category_source"] if m else ""

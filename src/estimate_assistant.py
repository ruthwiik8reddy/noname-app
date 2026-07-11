"""
estimate_assistant.py — lets the AI Assistant quote real prices
-----------------------------------------------------------------
Hooks into /assistant/query. If a question looks like a pricing/quote
question, we:
  1. pull the studio's REAL product catalog from the DB
  2. work out the vehicle's sqft (Gemini, cached)
  3. compute exact prices using the same formula as the estimator
  4. hand all of that to the AI as grounded facts

The AI answers in natural language using YOUR numbers — it never
invents a price. Non-pricing questions fall through untouched.
"""
import re
from typing import Optional

from .db_manager import get_db

QUOTE_TRIGGERS = [
    "quote", "price", "pricing", "cost", "how much", "estimate",
    "charge", "quotation", "what would", "ballpark", "rate for",
]

COVERAGE_HINTS = [
    (("full body", "full car", "whole car", "full ppf", "entire"), 1.0),
    (("front end", "front-end", "full front"), 0.55),
    (("partial front", "partial"), 0.35),
    (("hood and fender", "hood + fender", "hood only", "just the hood"), 0.20),
]


def looks_like_quote(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in QUOTE_TRIGGERS)


def guess_coverage(question: str) -> float:
    q = question.lower()
    for phrases, frac in COVERAGE_HINTS:
        if any(p in q for p in phrases):
            return frac
    return 1.0


# Words that are never part of a vehicle name
_NOISE = (
    r"full body|full car|whole car|full ppf|full front|front end|front-end|"
    r"partial front|partial|hood and fenders?|hood \+ fenders?|hood only|just the hood|"
    r"ppf|ceramic|graphene|coating|coatings|detail|detailing|wrap|paint|correction|"
    r"quote|quotation|price|pricing|cost|estimate|charge|how much|what would|ballpark|"
    r"me|us|please|for|on|to|a|an|my|the|of|is|it"
)

# Known makes help us anchor on the real vehicle
_MAKES = (
    "bmw|mercedes|merc|audi|porsche|tesla|toyota|honda|hyundai|kia|ford|chevrolet|chevy|"
    "nissan|mazda|subaru|volkswagen|vw|volvo|lexus|jaguar|land rover|range rover|mini|"
    "ferrari|lamborghini|maserati|bentley|rolls royce|aston martin|mclaren|jeep|dodge|"
    "ram|gmc|cadillac|lincoln|acura|infiniti|genesis|skoda|renault|peugeot|fiat|mahindra|"
    "tata|maruti|suzuki|mg|byd|rivian|lucid|polestar"
)


def extract_vehicle(question: str) -> Optional[str]:
    """Pull the vehicle out of a quote question.

    Strategy (most reliable first):
      1. year + make + model      e.g. "2023 BMW X7"
      2. known make + model       e.g. "BMW X7", "Tesla Model 3"
      3. after 'on/for', with noise words stripped
    """
    q = question.strip()

    # 1. Year + make + up to 3 model tokens
    m = re.search(
        r"((?:19|20)\d{2}\s+(?:" + _MAKES + r")(?:\s+[\w\-]+){0,3})",
        q, re.I)
    if m:
        return _clean(m.group(1))

    # 2. Known make + up to 3 model tokens
    m = re.search(
        r"\b((?:" + _MAKES + r")(?:\s+[\w\-]+){0,3})",
        q, re.I)
    if m:
        return _clean(m.group(1))

    # 3. Fall back: text after on/for, minus noise
    m = re.search(r"(?:on|for)\s+(?:a|an|my|the)?\s*([\w\-\s]{3,40})", q, re.I)
    if m:
        return _clean(m.group(1))

    # 4. Bare year + words
    m = re.search(r"((?:19|20)\d{2}\s+[\w\-]+(?:\s+[\w\-]+){0,2})", q)
    if m:
        return _clean(m.group(1))
    return None


def _clean(text: str) -> Optional[str]:
    """Strip service/coverage noise words off a candidate vehicle string."""
    t = text.strip(" ?.!,")
    # drop trailing noise words repeatedly
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"\b(" + _NOISE + r")\b", " ", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip(" ?.!,-")
    return t if len(t) >= 3 else None


def build_pricing_facts(studio_id: int, question: str) -> Optional[str]:
    """Grounded pricing facts for the AI, or None if not a quote question."""
    from .product_routes import _ai_estimate_sqft, _price

    if not looks_like_quote(question):
        return None

    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE studio_id=? AND active=1 ORDER BY category, name",
        (studio_id,)
    ).fetchall()
    if not products:
        return ("PRICING NOTE: This studio has no products in its catalog yet, so no "
                "exact quote can be given. Tell the user to add products under the "
                "Products menu first.")

    vehicle = extract_vehicle(question)
    coverage = guess_coverage(question)

    lines = ["=== STUDIO PRODUCT CATALOG (use ONLY these prices) ==="]
    for p in products:
        lines.append(
            f"- {p['name']} ({p['category']}"
            + (f", {p['brand']}" if p['brand'] else "")
            + f"): material ${p['cost_per_sqft']:.2f}/sqft, "
              f"labour ${p['labour_per_sqft']:.2f}/sqft, "
              f"markup {p['markup_percent']:.0f}%"
            + (f", {p['warranty_years']}yr warranty" if p['warranty_years'] else "")
        )

    if not vehicle:
        lines.append("")
        lines.append("No vehicle identified in the question. Ask the user which vehicle "
                     "they want quoted before giving a price.")
        return "\n".join(lines)

    sqft, source = _ai_estimate_sqft(vehicle, studio_id)
    area = round(sqft * coverage, 1)

    lines.append("")
    lines.append("=== VEHICLE ===")
    lines.append(f"{vehicle} — estimated {sqft:.0f} sqft total paintable area (source: {source})")
    lines.append(f"Coverage: {int(coverage*100)}% → {area} sqft treated")
    lines.append("")
    lines.append("=== EXACT COMPUTED QUOTES (correct — use verbatim) ===")
    for p in products:
        q = _price(p, sqft, coverage)
        lines.append(
            f"- {p['name']}: {q['area']} sqft → material ${q['material']:.2f} "
            f"+ labour ${q['labour']:.2f} + margin ${q['markup']:.2f} "
            f"= TOTAL ${q['total']:.2f}"
        )
    lines.append("")
    lines.append(
        "INSTRUCTIONS — ANSWER STYLE:\n"
        "• Give the PRICE and nothing else. Be direct and brief — 1 or 2 short sentences.\n"
        "• Use the TOTAL only. Do NOT show material cost, labour cost, markup, or any breakdown.\n"
        "• Do NOT explain how it was calculated. Do NOT add disclaimers or advice.\n"
        "• Do NOT recalculate or invent numbers — use the TOTAL exactly as given.\n"
        "• If only ONE product fits, reply like: "
        "\"Full PPF on a 2023 BMW X7 is $3,540 (XPEL Ultimate).\"\n"
        "• If SEVERAL products fit, list just name + price, one per line, nothing more."
    )
    return "\n".join(lines)

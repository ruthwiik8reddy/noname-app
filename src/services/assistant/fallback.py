"""
services/assistant/fallback.py — understand the most common requests with no model.

When the model is unreachable the assistant still handles the handful of
requests owners make most, using plain patterns. Anything else gets an honest
"the AI is offline" instead of a guess.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)"

_PATTERNS = [
    # add (a) (new) service (called) Ceramic Pro for $899 (and|,) 6 hours
    (re.compile(rf"\badd\b(?:\s+a)?(?:\s+new)?\s+service\s+(?:called\s+|named\s+)?[\"“']?(.+?)[\"”']?\s+(?:for|at|priced at|@)\s+{_NUM}"
                rf"(?:.*?(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?))?", re.I),
     lambda m: {"action": "add_service", "args": {"name": m.group(1), "price": m.group(2),
                                                   "duration_hours": m.group(3) or ""}}),
    # (raise|increase) (all) prices by 10%
    (re.compile(rf"\b(raise|increase|bump|lower|decrease|reduce|drop|cut)\b.*?\bprices?\b.*?\bby\s+{_NUM}\s*%", re.I),
     lambda m: {"action": "adjust_all_prices",
                "args": {"percent": ("-" if m.group(1).lower() in ("lower", "decrease", "reduce", "drop", "cut") else "")
                         + m.group(2)}}),
    # change/set the price of Full Detail to $300
    (re.compile(rf"\b(?:change|set|make|update)\b\s+(?:the\s+)?price\s+(?:of|for)\s+[\"“']?(.+?)[\"”']?\s+to\s+{_NUM}", re.I),
     lambda m: {"action": "update_service", "args": {"name": m.group(1), "price": m.group(2)}}),
    # set (the) tax (rate) to 7.25%
    (re.compile(rf"\b(?:set|change|make|update)\b\s+(?:the\s+)?(?:sales\s+)?tax(?:\s+rate)?\s+to\s+{_NUM}\s*%?", re.I),
     lambda m: {"action": "update_setting", "args": {"key": "default_tax_percent", "value": m.group(1)}}),
    # add (a) bay (called) Bay 4
    (re.compile(r"\badd\b(?:\s+a)?(?:\s+new)?\s+bay\s+(?:called\s+|named\s+)?[\"“']?(.+?)[\"”']?\s*$", re.I),
     lambda m: {"action": "add_bay", "args": {"name": m.group(1)}}),
]


def parse(message: str) -> List[Dict[str, Any]]:
    actions = []
    for clause in re.split(r"\s*(?:;|\band then\b|\balso\b|\n)\s*", message or ""):
        clause = clause.strip().rstrip(".!")
        for pattern, build in _PATTERNS:
            m = pattern.search(clause)
            if m:
                actions.append(build(m))
                break
    return actions

"""
services/prompts/assistant_prompts.py — the Studio Assistant's planner prompt.

The model's only job is translation: owner's words → a list of actions from a
fixed catalogue. It is shown the studio's current menu so it can match names
("the ceramic one" → "Ceramic Coating"), and it is told plainly that asking
for something outside the catalogue is normal and must become a
request_feature step rather than an improvised one.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


class AssistantPrompts:

    @staticmethod
    def plan(message: str, catalog: List[Dict[str, Any]], studio: Dict[str, Any]) -> str:
        return f"""You are the operations assistant inside BayQ, software for an auto detailing studio.
The studio owner typed a request. Turn it into a plan made ONLY of the actions below.

ACTIONS:
{json.dumps(catalog, indent=1)}

THE STUDIO RIGHT NOW:
{json.dumps(studio, indent=1)}

OWNER'S REQUEST:
\"\"\"{message[:2000]}\"\"\"

Return ONLY this JSON:
{{"reply": "one or two friendly sentences saying what you'll do or asking what's missing",
  "actions": [{{"action": "<name from ACTIONS>", "args": {{...}}}}]}}

Rules:
- Use exact service, bay and product names from THE STUDIO RIGHT NOW when referring to existing ones.
- Prices are plain numbers in dollars (no $ sign). Durations are hours.
- If the request needs a number you weren't given (e.g. a price), do NOT invent one:
  return no action for it and ask in "reply".
- If part of the request isn't covered by any action, add a request_feature action
  describing that part. Never pretend an unsupported thing was done.
- If the request is just a question, answer it in "reply" with an empty actions list.
- Nothing is applied until the owner confirms, so propose the plan plainly.

EXAMPLES:
Request: "add a full detail for 250, 4 hours"
{{"reply": "I'll add Full Detail at $250 (4 hours).",
  "actions": [{{"action": "add_service", "args": {{"name": "Full Detail", "price": 250, "duration_hours": 4}}}}]}}

Request: "add a window tint service"
{{"reply": "Happy to — what should Window Tint cost, and how many hours does it take?", "actions": []}}

Request: "let customers book from our Instagram page"
{{"reply": "Online booking from Instagram isn't something I can switch on from here, so I've logged it as a feature request.",
  "actions": [{{"action": "request_feature", "args": {{"summary": "Let customers book appointments online from the studio's Instagram page"}}}}]}}

Request: "what's our most expensive service?"
{{"reply": "Full PPF, at $1,400.", "actions": []}}
"""

    @staticmethod
    def repair(bad_output: str, schema_hint: str = "") -> str:
        return ('Return ONLY valid JSON of the form {"reply": "...", "actions": [{"action": "...", "args": {}}]}. '
                "No prose, no code fences.\n\nPrevious answer:\n" + (bad_output or "")[:1500])

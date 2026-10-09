# """
# ai_service.py — AI routing for BayQ assistant (100% Local / Ollama)
# ----------------------------------------------------
# Architecture: LocalRouter decides the route, AIService dispatches.

# Routing priority:
#   1. Attachments (image/video) → Ollama Vision (e.g., llava)
#   2. All other text/data tasks → Ollama Text (e.g., qwen2.5 or llama3.1)

# Cloud APIs (OpenAI/Gemini) have been completely removed for maximum privacy 
# and zero recurring API costs. Strict JSON formatting and Few-Shot prompting 
# are enforced to guarantee commercial-grade reliability from local models.
# """

# import json
# import os
# import sqlite3
# import base64
# from dataclasses import dataclass
# from enum import Enum
# from typing import Any, Dict, List, Optional
# import logging

# import requests

# from .config import Config

# logger = logging.getLogger(__name__)

# # ── Response model ────────────────────────────────────────────────────────────

# @dataclass
# class AIAnswer:
#     text:      str
#     reasoning: str
#     sources:   List[str]
#     evidence:  List[Dict[str, str]]

#     def as_dict(self) -> Dict[str, Any]:
#         return {
#             "answer":    self.text,
#             "reasoning": self.reasoning,
#             "sources":   self.sources,
#             "evidence":  self.evidence,
#         }


# class AIModel(Enum):
#     OLLAMA_TEXT = "ollama_text"
#     OLLAMA_VISION = "ollama_vision"


# # ── Routing logic ─────────────────────────────────────────────────────────────

# VISUAL_TRIGGERS = [
#     "photo", "image", "picture", "video", "look at", "see this",
#     "compare", "worn", "good", "bad", "looks like", "show me",
# ]

# class LocalRouter:
#     """Decides which local model should answer a given question."""

#     def __init__(self, config: Config):
#         self.config = config

#     def choose_route(self, question: str, attachments: Optional[List[str]]) -> AIModel:
#         text = question.lower()
#         has_attachment = bool(attachments)

#         if has_attachment or any(t in text for t in VISUAL_TRIGGERS):
#             return AIModel.OLLAMA_VISION

#         return AIModel.OLLAMA_TEXT


# # ── Vision analysis ───────────────────────────────────────────────────────────

# class VisionAnalyzer:
#     def __init__(self, config: Config):
#         self.config = config

#     def analyze(self, attachments: Optional[List[str]]) -> List[Dict[str, str]]:
#         if not attachments:
#             return []
#         results = []
#         for attachment in attachments:
#             label = os.path.basename(attachment)
#             ext = os.path.splitext(label)[1].lower()
#             item_type = "video" if ext in [".mp4", ".mov", ".avi"] else "image"
#             results.append({
#                 "attachment":  attachment,
#                 "type":        item_type,
#                 "description": f"Detected a {item_type} uploaded as {label}.",
#                 "confidence":  "high",
#             })
#         return results

#     def summarize(self, attachments: Optional[List[str]]) -> str:
#         if not attachments:
#             return ""
#         lines = [f"{i + 1}. {os.path.basename(a)}" for i, a in enumerate(attachments)]
#         return "Attachments sent for analysis:\n" + "\n".join(lines)


# # ── Studio memory ──────────────────────────────────────────────────────────────

# class UserMemoryStore:
#     def __init__(self, config: Config):
#         self.config = config

#     def _connect(self) -> sqlite3.Connection:
#         conn = sqlite3.connect(self.config.DB_PATH)
#         conn.row_factory = sqlite3.Row
#         return conn

#     def load_profile(self, studio_id: int) -> Dict[str, Any]:
#         conn = self._connect()
#         row = conn.execute("SELECT * FROM studios WHERE id=?", (studio_id,)).fetchone()
#         conn.close()
#         if not row:
#             return {}
#         return {"studio_name": row["name"], "city": row["city"], "owner": row["owner"]}

#     def load_jobs(self, studio_id: int) -> List[Dict]:
#         conn = self._connect()
#         rows = conn.execute(
#             "SELECT car, service, status, technician, price FROM jobs "
#             "WHERE studio_id=? ORDER BY id DESC LIMIT 5", (studio_id,)
#         ).fetchall()
#         conn.close()
#         return [dict(r) for r in rows]

#     def load_services(self, studio_id: int) -> List[Dict]:
#         conn = self._connect()
#         rows = conn.execute(
#             "SELECT name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
#         ).fetchall()
#         conn.close()
#         return [dict(r) for r in rows]

#     def load_recent_bookings(self, studio_id: int) -> List[Dict]:
#         conn = self._connect()
#         rows = conn.execute(
#             "SELECT customer_name, vehicle, date, time_slot, status FROM bookings "
#             "WHERE studio_id=? ORDER BY date DESC LIMIT 3", (studio_id,)
#         ).fetchall()
#         conn.close()
#         return [dict(r) for r in rows]

#     def load_recent_estimates(self, studio_id: int) -> List[Dict]:
#         conn = self._connect()
#         rows = conn.execute(
#             "SELECT customer_name, total, status FROM estimates "
#             "WHERE studio_id=? ORDER BY created_at DESC LIMIT 3", (studio_id,)
#         ).fetchall()
#         conn.close()
#         return [dict(r) for r in rows]

#     def build_context(self, studio_id: int, question: str) -> Dict[str, Any]:
#         q = question.lower()
#         context: Dict[str, Any] = {"profile": self.load_profile(studio_id)}
#         if any(w in q for w in ["job", "car", "service", "tech", "status"]):
#             context["recent_jobs"] = self.load_jobs(studio_id)
#         if any(w in q for w in ["service", "package", "price", "cost", "offer"]):
#             context["services"] = self.load_services(studio_id)
#         if any(w in q for w in ["booking", "appointment", "schedule", "slot"]):
#             context["recent_bookings"] = self.load_recent_bookings(studio_id)
#         if any(w in q for w in ["estimate", "quote", "total", "invoice"]):
#             context["recent_estimates"] = self.load_recent_estimates(studio_id)
#         return context


# # ── Prompt builder ────────────────────────────────────────────────────────────

# class PromptBuilder:
#     SYSTEM = (
#         "You are an AI assistant built into BayQ, a detailing studio management platform. "
#         "You help studio owners and staff with questions about their business, services, and industry knowledge. "
#         "Be concise, practical, and professional. "
#         "When referencing studio data, cite it clearly. "
#         "If you don't know something, say so honestly."
#     )

#     @staticmethod
#     def build(
#         question: str,
#         context: Dict[str, Any],
#         visual_summary: str = "",
#         visual_evidence: Optional[List[Dict[str, str]]] = None
#     ) -> str:
#         parts = [PromptBuilder.SYSTEM, ""]

#         profile = context.get("profile", {})
#         if profile:
#             parts.append(f"Studio: {profile.get('studio_name')} — {profile.get('city')}")
#             parts.append(f"Owner: {profile.get('owner')}")
#             parts.append("")

#         jobs = context.get("recent_jobs", [])
#         if jobs:
#             parts.append("Recent jobs:")
#             for j in jobs:
#                 parts.append(f"  - {j['car']} | {j['service']} | {j['status']} | ${j['price']}")
#             parts.append("")

#         services = context.get("services", [])
#         if services:
#             parts.append("Services offered:")
#             for s in services:
#                 parts.append(f"  - {s['name']}: ${s['price']} ({s['duration_hr']}hrs)")
#             parts.append("")

#         bookings = context.get("recent_bookings", [])
#         if bookings:
#             parts.append("Recent bookings:")
#             for b in bookings:
#                 parts.append(f"  - {b['customer_name']} | {b['vehicle']} | {b['date']} {b['time_slot']} | {b['status']}")
#             parts.append("")

#         estimates = context.get("recent_estimates", [])
#         if estimates:
#             parts.append("Recent estimates:")
#             for e in estimates:
#                 parts.append(f"  - {e['customer_name']} | ${e['total']/100:.2f} | {e['status']}")
#             parts.append("")

#         if visual_summary:
#             parts.append("Visual summary:")
#             parts.append(visual_summary)
#             parts.append("")

#         parts.append(f"Question: {question}")
#         parts.append("Answer:")
#         return "\n".join(parts)


# # ── Strict Local Backend (Ollama) ─────────────────────────────────────────────

# class OllamaBackend:
#     def __init__(self, config: Config):
#         self.config = config

#     def call(self, prompt: str, enforce_json: bool = False, images: Optional[List[str]] = None, model_override: str = None) -> Optional[str]:
#         if not self.config.OLLAMA_URL:
#             print("[Ollama] Error: OLLAMA_URL is not set in config.")
#             return None
            
#         # Use vision model if requested, else default text model
#         model = model_override or self.config.OLLAMA_MODEL
        
#         payload = {
#             "model": model,
#             "prompt": prompt, 
#             "stream": False,
#             "options": {
#                 "temperature": 0.1 # Crucial for strict adherence and logic
#             }
#         }
        
#         # Enforce strict JSON schema matching 
#         if enforce_json:
#             payload["format"] = "json"

#         # Handle image attachments for vision models (like llava)
#         if images:
#             base64_images = []
#             for img_path in images:
#                 try:
#                     if os.path.exists(img_path):
#                         with open(img_path, "rb") as img_file:
#                             base64_images.append(base64.b64encode(img_file.read()).decode('utf-8'))
#                 except Exception as e:
#                     print(f"[Ollama] Failed to load image {img_path}: {e}")
#             if base64_images:
#                 payload["images"] = base64_images

#         try:
#             resp = requests.post(
#                 f"{self.config.OLLAMA_URL.rstrip('/')}/api/generate",
#                 json=payload,
#                 timeout=60, # Local models need more time, especially for vision/JSON
#             )
#             resp.raise_for_status()
#             return resp.json().get("response", "").strip()
#         except Exception as e:
#             print(f"[Ollama] Request failed: {e}")
#             return None


# # ── Main service ──────────────────────────────────────────────────────────────

# class AIService:
#     def __init__(self):
#         self.config = Config
#         self.router = LocalRouter(self.config)
#         self.memory = UserMemoryStore(self.config)
#         self.vision = VisionAnalyzer(self.config)
#         self.ollama = OllamaBackend(self.config)

#     def answer(self, studio_id: int, question: str, attachments: Optional[List[str]] = None) -> AIAnswer:
#         route   = self.router.choose_route(question, attachments)
#         context = self.memory.build_context(studio_id, question)

#         visual_summary  = self.vision.summarize(attachments)
#         visual_evidence = self.vision.analyze(attachments)

#         prompt = PromptBuilder.build(
#             question, context,
#             visual_summary=visual_summary,
#             visual_evidence=visual_evidence
#         )
#         return self._dispatch(route, prompt, visual_evidence, attachments)

#     def _dispatch(self, route: AIModel, prompt: str, evidence: List[Dict[str, str]], attachments: Optional[List[str]] = None) -> AIAnswer:
        
#         if route == AIModel.OLLAMA_VISION:
#             # We use a vision model override (e.g., 'llava') if attachments are present
#             vision_model = getattr(self.config, 'OLLAMA_VISION_MODEL', 'llava')
#             text = self.ollama.call(prompt, images=attachments, model_override=vision_model)
#             if text:
#                 return AIAnswer(text, "Analyzed locally using Vision model.", [f"ollama:{vision_model}"], evidence)
        
#         # Default Text Route
#         text = self.ollama.call(prompt)
#         if text:
#             return AIAnswer(text, "Answered by local text model.", [f"ollama:{self.config.OLLAMA_MODEL}"], evidence)
            
#         return AIAnswer(
#             text="Ollama is currently unreachable. Please check if the local server is running.",
#             reasoning="Connection failed.",
#             sources=["none"],
#             evidence=evidence,
#         )
    
#     def generate_json_estimate(self, studio_id: int, customer_notes: str) -> Dict[str, Any]:
#         """
#         Takes raw customer notes, injects real live inventory, and returns a structured JSON estimate.
#         Uses FEW-SHOT prompting to guarantee perfect structure from local models.
#         """
#         from .db_manager import get_catalog_for_ai_context
        
#         catalog = get_catalog_for_ai_context(studio_id)
#         if not catalog:
#             return {"error": "Your studio has no services configured. Please add services first."}
            
#         catalog_json = json.dumps(catalog)

#         # Few-Shot Prompting: Teach the model exactly what we expect
#         system_prompt = f"""You are an expert service estimator for an automotive detailing studio.
#         Read the customer's notes and generate a line-item estimate in perfect JSON.

#         STRICT RULES:
#         1. You may ONLY use items from the Active Catalog. Do not invent products.
#         2. Output MUST be valid JSON matching the exact schema provided.
#         3. Convert the integer price (cents) to a standard decimal (e.g., 50000 -> 500.00).

#         ACTIVE CATALOG:
#         {catalog_json}

#         EXAMPLE INPUT: "Customer has a scratched hood, needs polish and ceramic."
#         EXAMPLE OUTPUT:
#         {{
#             "estimate_summary": "We recommend a stage-1 polish and ceramic coating to restore and protect the hood.",
#             "line_items": [
#                 {{"name": "Stage 1 Polish", "unit_price": 250.00, "quantity": 1, "reason": "To remove hood scratches"}},
#                 {{"name": "Ceramic Coating", "unit_price": 800.00, "quantity": 1, "reason": "To protect the corrected paint"}}
#             ],
#             "total_estimated_price": 1050.00
#         }}
#         """

#         prompt = system_prompt + f"\n\nACTUAL INPUT: {customer_notes}\n\nReturn ONLY valid JSON."

#         # Pass enforce_json=True to force Ollama into JSON mode
#         raw_output = self.ollama.call(prompt, enforce_json=True)

#         if not raw_output:
#             return {"error": "Failed to generate estimate. Local AI is unavailable."}

#         try:
#             return json.loads(raw_output)
#         except json.JSONDecodeError:
#             return {"error": "Failed to parse AI estimate into JSON. Please try again.", "raw": raw_output}
        
#     def analyze_inventory_health(self, studio_id: int) -> Dict[str, Any]:
#         from .db_manager import get_db, get_studio_inventory, get_inventory_consumption

#         conn = get_db()
#         inventory = get_studio_inventory(studio_id)
#         if not inventory:
#             return {"error": "No inventory items found."}

#         consumption = get_inventory_consumption(studio_id)

#         # Trim inventory to essential fields
#         trimmed_inventory = []
#         for item in inventory:
#             trimmed_inventory.append({
#                 "sku": item["sku"],
#                 "name": item["name"],
#                 "category": item["category"],
#                 "qty": item["quantity"],
#                 "reorder": item["reorder_level"],
#                 "consumed_30d": consumption.get(item["id"], 0)  # new field
#             })

#         # Limit recent jobs to last 10
#         recent_jobs = conn.execute(
#             "SELECT service, car FROM jobs WHERE studio_id=? AND status='Completed' "
#             "ORDER BY completed_at DESC LIMIT 10", (studio_id,)
#         ).fetchall()

#         upcoming_bookings = conn.execute(
#             "SELECT customer_name, vehicle, date FROM bookings WHERE studio_id=? "
#             "AND status='Pending' ORDER BY date ASC LIMIT 10", (studio_id,)
#         ).fetchall()

#         context_data = {
#             "inventory": trimmed_inventory,
#             "recent_completed_jobs": [dict(j) for j in recent_jobs],
#             "upcoming_bookings": [dict(b) for b in upcoming_bookings]
#         }

#         # Few‑shot prompt with explicit health rules
#         system_prompt = f"""
#     You are an AI Inventory & Operations Analyst for a detailing studio.

#     INVENTORY & ACTIVITY CONTEXT:
#     {json.dumps(context_data, indent=2)}

#     YOUR GOAL: Generate an operational & financial inventory report in valid JSON.

#     RULES FOR HEALTH_SCORE:
#     - If more than 40% of items are below reorder level → "Critical"
#     - If more than 20% are below reorder level → "Attention Needed"
#     - Otherwise → "Good"

#     REQUIRED JSON SCHEMA:
#     {{
#         "health_score": "Good / Attention Needed / Critical",
#         "summary": "1‑2 sentence executive overview.",
#         "urgent_reorders": [
#             {{"sku": "...", "name": "...", "current_qty": 0, "reorder_level": 0,
#             "suggested_order_qty": 0, "reason": "..."}}
#         ],
#         "consumption_forecast": [
#             {{"category": "...", "trend": "High / Stable / Low",
#             "insight": "..."}}
#         ],
#         "cost_optimizations": ["Tip 1", "Tip 2"]
#     }}
#     Return ONLY valid JSON.
#     """
#         raw_output = self.ollama.call(system_prompt, enforce_json=True)
#         if not raw_output:
#             return {"error": "AI service unavailable."}
#         try:
#             return json.loads(raw_output)
#         except json.JSONDecodeError:
#             return {"error": "Failed to parse AI response.", "raw": raw_output}
        














"""
ai_service.py — AI routing for BayQ assistant (100% Local / Ollama)
----------------------------------------------------
Architecture: LocalRouter decides the route, AIService dispatches.

Routing priority:
  1. Attachments (image/video) → Ollama Vision (e.g., llava)
  2. All other text/data tasks → Ollama Text (e.g., qwen2.5 or llama3.1)

Cloud APIs (OpenAI/Gemini) have been completely removed for maximum privacy 
and zero recurring API costs. Strict JSON formatting and Few-Shot prompting 
are enforced to guarantee commercial-grade reliability from local models.
"""

import json
import os
import sqlite3
import base64
import time  # 👈 NEW (for cache TTL)
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional
import logging

import requests

from .config import Config

logger = logging.getLogger(__name__)

# ── Response model ────────────────────────────────────────────────────────────

@dataclass
class AIAnswer:
    text:      str
    reasoning: str
    sources:   List[str]
    evidence:  List[Dict[str, str]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "answer":    self.text,
            "reasoning": self.reasoning,
            "sources":   self.sources,
            "evidence":  self.evidence,
        }


class AIModel(Enum):
    OLLAMA_TEXT = "ollama_text"
    OLLAMA_VISION = "ollama_vision"


# ── Routing logic ─────────────────────────────────────────────────────────────

VISUAL_TRIGGERS = [
    "photo", "image", "picture", "video", "look at", "see this",
    "compare", "worn", "good", "bad", "looks like", "show me",
]

class LocalRouter:
    """Decides which local model should answer a given question."""

    def __init__(self, config: Config):
        self.config = config

    def choose_route(self, question: str, attachments: Optional[List[str]]) -> AIModel:
        text = question.lower()
        has_attachment = bool(attachments)

        if has_attachment or any(t in text for t in VISUAL_TRIGGERS):
            return AIModel.OLLAMA_VISION

        return AIModel.OLLAMA_TEXT


# ── Vision analysis ───────────────────────────────────────────────────────────

class VisionAnalyzer:
    def __init__(self, config: Config):
        self.config = config

    def analyze(self, attachments: Optional[List[str]]) -> List[Dict[str, str]]:
        if not attachments:
            return []
        results = []
        for attachment in attachments:
            label = os.path.basename(attachment)
            ext = os.path.splitext(label)[1].lower()
            item_type = "video" if ext in [".mp4", ".mov", ".avi"] else "image"
            results.append({
                "attachment":  attachment,
                "type":        item_type,
                "description": f"Detected a {item_type} uploaded as {label}.",
                "confidence":  "high",
            })
        return results

    def summarize(self, attachments: Optional[List[str]]) -> str:
        if not attachments:
            return ""
        lines = [f"{i + 1}. {os.path.basename(a)}" for i, a in enumerate(attachments)]
        return "Attachments sent for analysis:\n" + "\n".join(lines)


# ── Studio memory ──────────────────────────────────────────────────────────────

class UserMemoryStore:
    def __init__(self, config: Config):
        self.config = config

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def load_profile(self, studio_id: int) -> Dict[str, Any]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM studios WHERE id=?", (studio_id,)).fetchone()
        conn.close()
        if not row:
            return {}
        return {"studio_name": row["name"], "city": row["city"], "owner": row["owner"]}

    def load_jobs(self, studio_id: int) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT car, service, status, technician, price FROM jobs "
            "WHERE studio_id=? ORDER BY id DESC LIMIT 5", (studio_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def load_services(self, studio_id: int) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def load_recent_bookings(self, studio_id: int) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT customer_name, vehicle, date, time_slot, status FROM bookings "
            "WHERE studio_id=? ORDER BY date DESC LIMIT 3", (studio_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def load_recent_estimates(self, studio_id: int) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT customer_name, total, status FROM estimates "
            "WHERE studio_id=? ORDER BY created_at DESC LIMIT 3", (studio_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def build_context(self, studio_id: int, question: str) -> Dict[str, Any]:
        q = question.lower()
        context: Dict[str, Any] = {"profile": self.load_profile(studio_id)}
        if any(w in q for w in ["job", "car", "service", "tech", "status"]):
            context["recent_jobs"] = self.load_jobs(studio_id)
        if any(w in q for w in ["service", "package", "price", "cost", "offer"]):
            context["services"] = self.load_services(studio_id)
        if any(w in q for w in ["booking", "appointment", "schedule", "slot"]):
            context["recent_bookings"] = self.load_recent_bookings(studio_id)
        if any(w in q for w in ["estimate", "quote", "total", "invoice"]):
            context["recent_estimates"] = self.load_recent_estimates(studio_id)
        return context


# ── Prompt builder ────────────────────────────────────────────────────────────

class PromptBuilder:
    SYSTEM = (
        "You are an AI assistant built into BayQ, a detailing studio management platform. "
        "You help studio owners and staff with questions about their business, services, and industry knowledge. "
        "Be concise, practical, and professional. "
        "When referencing studio data, cite it clearly. "
        "If you don't know something, say so honestly."
    )

    @staticmethod
    def build(
        question: str,
        context: Dict[str, Any],
        visual_summary: str = "",
        visual_evidence: Optional[List[Dict[str, str]]] = None
    ) -> str:
        parts = [PromptBuilder.SYSTEM, ""]

        profile = context.get("profile", {})
        if profile:
            parts.append(f"Studio: {profile.get('studio_name')} — {profile.get('city')}")
            parts.append(f"Owner: {profile.get('owner')}")
            parts.append("")

        jobs = context.get("recent_jobs", [])
        if jobs:
            parts.append("Recent jobs:")
            for j in jobs:
                parts.append(f"  - {j['car']} | {j['service']} | {j['status']} | ${j['price']}")
            parts.append("")

        services = context.get("services", [])
        if services:
            parts.append("Services offered:")
            for s in services:
                parts.append(f"  - {s['name']}: ${s['price']} ({s['duration_hr']}hrs)")
            parts.append("")

        bookings = context.get("recent_bookings", [])
        if bookings:
            parts.append("Recent bookings:")
            for b in bookings:
                parts.append(f"  - {b['customer_name']} | {b['vehicle']} | {b['date']} {b['time_slot']} | {b['status']}")
            parts.append("")

        estimates = context.get("recent_estimates", [])
        if estimates:
            parts.append("Recent estimates:")
            for e in estimates:
                parts.append(f"  - {e['customer_name']} | ${e['total']/100:.2f} | {e['status']}")
            parts.append("")

        if visual_summary:
            parts.append("Visual summary:")
            parts.append(visual_summary)
            parts.append("")

        parts.append(f"Question: {question}")
        parts.append("Answer:")
        return "\n".join(parts)


# ── Strict Local Backend (Ollama) ─────────────────────────────────────────────

class OllamaBackend:
    def __init__(self, config: Config):
        self.config = config

    def call(self, prompt: str, enforce_json: bool = False, images: Optional[List[str]] = None, model_override: str = None) -> Optional[str]:
        if not self.config.OLLAMA_URL:
            print("[Ollama] Error: OLLAMA_URL is not set in config.")
            return None
            
        # Use vision model if requested, else default text model
        model = model_override or self.config.OLLAMA_MODEL
        
        payload = {
            "model": model,
            "prompt": prompt, 
            "stream": False,
            "options": {
                "temperature": 0.1 # Crucial for strict adherence and logic
            }
        }
        
        # Enforce strict JSON schema matching 
        if enforce_json:
            payload["format"] = "json"

        # Handle image attachments for vision models (like llava)
        if images:
            base64_images = []
            for img_path in images:
                try:
                    if os.path.exists(img_path):
                        with open(img_path, "rb") as img_file:
                            base64_images.append(base64.b64encode(img_file.read()).decode('utf-8'))
                except Exception as e:
                    print(f"[Ollama] Failed to load image {img_path}: {e}")
            if base64_images:
                payload["images"] = base64_images

        try:
            resp = requests.post(
                f"{self.config.OLLAMA_URL.rstrip('/')}/api/generate",
                json=payload,
                timeout=60, # Local models need more time, especially for vision/JSON
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            print(f"[Ollama] Request failed: {e}")
            return None


class GeminiBackend:
    """Stub class – we only use local Ollama."""
    def __init__(self, config):
        self.config = config

    def call(self, prompt: str) -> Optional[str]:
        # Always return None so we fall back to Ollama
        return None
# ── Main service ──────────────────────────────────────────────────────────────

class AIService:
    # 👇 NEW: Simple in-memory cache
    _cache = {}  # key: (studio_id, "inventory_health") -> (timestamp, result)

    @staticmethod
    def invalidate_cache(studio_id: int):
        """Call this whenever inventory changes for a studio."""
        AIService._cache.pop((studio_id, "inventory_health"), None)

    def __init__(self):
        self.config = Config
        self.router = LocalRouter(self.config)
        self.memory = UserMemoryStore(self.config)
        self.vision = VisionAnalyzer(self.config)
        self.ollama = OllamaBackend(self.config)

    def answer(self, studio_id: int, question: str, attachments: Optional[List[str]] = None) -> AIAnswer:
        route   = self.router.choose_route(question, attachments)
        context = self.memory.build_context(studio_id, question)

        visual_summary  = self.vision.summarize(attachments)
        visual_evidence = self.vision.analyze(attachments)

        prompt = PromptBuilder.build(
            question, context,
            visual_summary=visual_summary,
            visual_evidence=visual_evidence
        )
        return self._dispatch(route, prompt, visual_evidence, attachments)

    def _dispatch(self, route: AIModel, prompt: str, evidence: List[Dict[str, str]], attachments: Optional[List[str]] = None) -> AIAnswer:
        
        if route == AIModel.OLLAMA_VISION:
            # We use a vision model override (e.g., 'llava') if attachments are present
            vision_model = getattr(self.config, 'OLLAMA_VISION_MODEL', 'llava')
            text = self.ollama.call(prompt, images=attachments, model_override=vision_model)
            if text:
                return AIAnswer(text, "Analyzed locally using Vision model.", [f"ollama:{vision_model}"], evidence)
        
        # Default Text Route
        text = self.ollama.call(prompt)
        if text:
            return AIAnswer(text, "Answered by local text model.", [f"ollama:{self.config.OLLAMA_MODEL}"], evidence)
            
        return AIAnswer(
            text="Ollama is currently unreachable. Please check if the local server is running.",
            reasoning="Connection failed.",
            sources=["none"],
            evidence=evidence,
        )
    
    def generate_json_estimate(self, studio_id: int, customer_notes: str) -> Dict[str, Any]:
        """
        Takes raw customer notes, injects real live inventory, and returns a structured JSON estimate.
        Uses FEW-SHOT prompting to guarantee perfect structure from local models.
        """
        from .db_manager import get_catalog_for_ai_context
        
        catalog = get_catalog_for_ai_context(studio_id)
        if not catalog:
            return {"error": "Your studio has no services configured. Please add services first."}
            
        catalog_json = json.dumps(catalog)

        # Few-Shot Prompting: Teach the model exactly what we expect
        system_prompt = f"""You are an expert service estimator for an automotive detailing studio.
        Read the customer's notes and generate a line-item estimate in perfect JSON.

        STRICT RULES:
        1. You may ONLY use items from the Active Catalog. Do not invent products.
        2. Output MUST be valid JSON matching the exact schema provided.
        3. Convert the integer price (cents) to a standard decimal (e.g., 50000 -> 500.00).

        ACTIVE CATALOG:
        {catalog_json}

        EXAMPLE INPUT: "Customer has a scratched hood, needs polish and ceramic."
        EXAMPLE OUTPUT:
        {{
            "estimate_summary": "We recommend a stage-1 polish and ceramic coating to restore and protect the hood.",
            "line_items": [
                {{"name": "Stage 1 Polish", "unit_price": 250.00, "quantity": 1, "reason": "To remove hood scratches"}},
                {{"name": "Ceramic Coating", "unit_price": 800.00, "quantity": 1, "reason": "To protect the corrected paint"}}
            ],
            "total_estimated_price": 1050.00
        }}
        """

        prompt = system_prompt + f"\n\nACTUAL INPUT: {customer_notes}\n\nReturn ONLY valid JSON."

        # Pass enforce_json=True to force Ollama into JSON mode
        raw_output = self.ollama.call(prompt, enforce_json=True)

        if not raw_output:
            return {"error": "Failed to generate estimate. Local AI is unavailable."}

        try:
            return json.loads(raw_output)
        except json.JSONDecodeError:
            return {"error": "Failed to parse AI estimate into JSON. Please try again.", "raw": raw_output}
        
    def analyze_inventory_health(self, studio_id: int, force_refresh: bool = False) -> Dict[str, Any]:
        # 👇 NEW: Cache check at the start
        cache_key = (studio_id, "inventory_health")
        now = time.time()
        if not force_refresh and cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if now - ts < 300:  # 5 minutes
                return data

        # ── YOUR EXISTING LOGIC STARTS HERE (100% unchanged) ──
        from .db_manager import get_db, get_studio_inventory, get_inventory_consumption

        conn = get_db()
        inventory = get_studio_inventory(studio_id)
        if not inventory:
            return {"error": "No inventory items found."}

        consumption = get_inventory_consumption(studio_id)

        # Trim inventory to essential fields
        trimmed_inventory = []
        for item in inventory:
            trimmed_inventory.append({
                "sku": item["sku"],
                "name": item["name"],
                "category": item["category"],
                "qty": item["quantity"],
                "reorder": item["reorder_level"],
                "consumed_30d": consumption.get(item["id"], 0)
            })

        # Limit recent jobs to last 10
        recent_jobs = conn.execute(
            "SELECT service, car FROM jobs WHERE studio_id=? AND status='Completed' "
            "ORDER BY completed_at DESC LIMIT 10", (studio_id,)
        ).fetchall()

        upcoming_bookings = conn.execute(
            "SELECT customer_name, vehicle, date FROM bookings WHERE studio_id=? "
            "AND status='Pending' ORDER BY date ASC LIMIT 10", (studio_id,)
        ).fetchall()

        context_data = {
            "inventory": trimmed_inventory,
            "recent_completed_jobs": [dict(j) for j in recent_jobs],
            "upcoming_bookings": [dict(b) for b in upcoming_bookings]
        }

        system_prompt = f"""
    You are an AI Inventory & Operations Analyst for a detailing studio.

    INVENTORY & ACTIVITY CONTEXT:
    {json.dumps(context_data, indent=2)}

    YOUR GOAL: Generate an operational & financial inventory report in valid JSON.

    RULES FOR HEALTH_SCORE:
    - If more than 40% of items are below reorder level → "Critical"
    - If more than 20% are below reorder level → "Attention Needed"
    - Otherwise → "Good"

    REQUIRED JSON SCHEMA:
    {{
        "health_score": "Good / Attention Needed / Critical",
        "summary": "1‑2 sentence executive overview.",
        "urgent_reorders": [
            {{"sku": "...", "name": "...", "current_qty": 0, "reorder_level": 0,
            "suggested_order_qty": 0, "reason": "..."}}
        ],
        "consumption_forecast": [
            {{"category": "...", "trend": "High / Stable / Low",
            "insight": "..."}}
        ],
        "cost_optimizations": ["Tip 1", "Tip 2"]
    }}
    Return ONLY valid JSON.
    """
        raw_output = self.ollama.call(system_prompt, enforce_json=True)
        if not raw_output:
            return {"error": "AI service unavailable."}
        try:
            result = json.loads(raw_output)
        except json.JSONDecodeError:
            return {"error": "Failed to parse AI response.", "raw": raw_output}

        # 👇 NEW: Cache store at the end
        self._cache[cache_key] = (time.time(), result)
        return result

        
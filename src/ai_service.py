"""
ai_service.py — AI routing for Autofiera assistant
----------------------------------------------------
Architecture: LocalRouter decides the route, AIService dispatches.

Routing priority:
  1. Attachments (image/video) → Gemini (vision) → OpenAI → local fallback
  2. Visual/comparison keywords  → Gemini → OpenAI → local fallback
  3. Simple/studio-context questions → Ollama (local, free, fast)
  4. Complex/research questions      → Gemini → Ollama fallback

Ollama is the default workhorse for anything that doesn't need internet
or vision — it's free and fast. Gemini is reserved for vision, research,
and "latest/trending/compare" style questions. OpenAI stays available
as an optional third backend if a key is set.
"""

import json
import os
import sqlite3
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
    OLLAMA = "ollama"
    OPENAI = "openai"
    GEMINI = "gemini"


# ── Routing logic ─────────────────────────────────────────────────────────────

RESEARCH_TRIGGERS = [
    "latest", "trending", "news", "2024", "2025", "2026",
    "price of", "how much does", "best brand", "review",
    "compare brands", "research", "what is the difference between",
    "industry standard", "market", "recommend a product",
    "how does ceramic coating work", "how does ppf work",
    "what causes", "science behind", "chemical",
]

VISUAL_TRIGGERS = [
    "photo", "image", "picture", "video", "look at", "see this",
    "compare", "worn", "good", "bad", "looks like", "show me",
]


class LocalRouter:
    """Decides which backend should answer a given question."""

    def __init__(self, config: Config):
        self.config = config

    def choose_route(self, question: str, attachments: Optional[List[str]]) -> AIModel:
        text = question.lower()
        has_attachment = bool(attachments)

        if has_attachment or any(t in text for t in VISUAL_TRIGGERS):
            if self.config.GEMINI_API_KEY:
                return AIModel.GEMINI
            if self.config.OPENAI_API_KEY:
                return AIModel.OPENAI
            return AIModel.OLLAMA

        if any(t in text for t in RESEARCH_TRIGGERS):
            if self.config.GEMINI_API_KEY:
                return AIModel.GEMINI
            return AIModel.OLLAMA

        return AIModel.OLLAMA


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
                "confidence":  "low",
            })
        return results

    def summarize(self, attachments: Optional[List[str]]) -> str:
        if not attachments:
            return ""
        lines = [f"{i + 1}. {os.path.basename(a)}" for i, a in enumerate(attachments)]
        return "Attachments sent for analysis:\n" + "\n".join(lines)

    def compare_to_user_data(self, studio_id: int, attachments: Optional[List[str]]) -> List[Dict[str, str]]:
        if not attachments:
            return []
        return [{
            "attachment": a,
            "note": (
                "Visual comparison requested. For precise analysis, "
                "ensure GEMINI_API_KEY is configured for vision support."
            ),
        } for a in attachments]


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
        "You are an AI assistant built into Autofiera, a detailing studio management platform. "
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
        visual_evidence: Optional[List[Dict[str, str]]] = None,
        research_mode: bool = False,
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

        if visual_evidence:
            parts.append("Attachment evidence:")
            for item in visual_evidence:
                parts.append(f"  - {item['attachment']} ({item.get('type','')}): {item.get('description','')}")
            parts.append("")

        if research_mode:
            parts.append("Note: Use your internet knowledge and reasoning to answer fully.")
            parts.append("")

        parts.append(f"Question: {question}")
        parts.append("Answer:")
        return "\n".join(parts)


# ── AI backends ───────────────────────────────────────────────────────────────

class OllamaBackend:
    def __init__(self, config: Config):
        self.config = config

    def call(self, prompt: str) -> Optional[str]:
        if not self.config.OLLAMA_URL:
            return None
        try:
            resp = requests.post(
                f"{self.config.OLLAMA_URL.rstrip('/')}/api/generate",
                json={"model": self.config.OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            print(f"[Ollama] Failed: {e}")
            return None


class GeminiBackend:
    def __init__(self, config: Config):
        self.config = config

    def call(self, prompt: str) -> Optional[str]:
        if not self.config.GEMINI_API_KEY:
            return None
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.config.GEMINI_MODEL}:generateContent?key={self.config.GEMINI_API_KEY}"
            )
            resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return " ".join(p.get("text", "") for p in parts).strip()
            return None
        except Exception as e:
            print(f"[Gemini] Failed: {e}")
            return None


class OpenAIBackend:
    def __init__(self, config: Config):
        self.config = config

    def call(self, prompt: str) -> Optional[str]:
        if not self.config.OPENAI_API_KEY:
            return None
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.config.OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": self.config.OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a technical assistant for Autofiera."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 600,
                },
                timeout=25,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[OpenAI] Failed: {e}")
            return None


# ── Main service ──────────────────────────────────────────────────────────────

class AIService:
    def __init__(self):
        self.config = Config
        self.router = LocalRouter(self.config)
        self.memory = UserMemoryStore(self.config)
        self.vision = VisionAnalyzer(self.config)
        self.ollama = OllamaBackend(self.config)
        self.gemini = GeminiBackend(self.config)
        self.openai = OpenAIBackend(self.config)

    def answer(self, studio_id: int, question: str, attachments: Optional[List[str]] = None) -> AIAnswer:
        route   = self.router.choose_route(question, attachments)
        context = self.memory.build_context(studio_id, question)

        visual_summary  = self.vision.summarize(attachments)
        visual_evidence = self.vision.analyze(attachments)
        comparison_data = self.vision.compare_to_user_data(studio_id, attachments)
        research_mode   = route in (AIModel.GEMINI, AIModel.OPENAI)

        prompt = PromptBuilder.build(
            question, context,
            visual_summary=visual_summary,
            visual_evidence=visual_evidence,
            research_mode=research_mode,
        )
        return self._dispatch(route, prompt, comparison_data)

    def _dispatch(self, route: AIModel, prompt: str, evidence: List[Dict[str, str]]) -> AIAnswer:
        if route == AIModel.GEMINI:
            text = self.gemini.call(prompt)
            if text:
                return AIAnswer(text, "Answered by Gemini.", [f"gemini:{self.config.GEMINI_MODEL}"], evidence)
            print("[AIService] Gemini unavailable, falling back to Ollama")
            text = self.ollama.call(prompt)
            if text:
                return AIAnswer(text, "Gemini unavailable — answered by Ollama.", [f"ollama:{self.config.OLLAMA_MODEL}"], evidence)
            return self._no_backend_response(evidence)

        if route == AIModel.OPENAI:
            text = self.openai.call(prompt)
            if text:
                return AIAnswer(text, "Answered by OpenAI.", [f"openai:{self.config.OPENAI_MODEL}"], evidence)
            print("[AIService] OpenAI unavailable, falling back to Ollama")
            text = self.ollama.call(prompt)
            if text:
                return AIAnswer(text, "OpenAI unavailable — answered by Ollama.", [f"ollama:{self.config.OLLAMA_MODEL}"], evidence)
            return self._no_backend_response(evidence)

        # Default: OLLAMA first, Gemini fallback
        text = self.ollama.call(prompt)
        if text:
            return AIAnswer(text, "Answered by local Ollama model.", [f"ollama:{self.config.OLLAMA_MODEL}"], evidence)
        print("[AIService] Ollama unavailable, falling back to Gemini")
        text = self.gemini.call(prompt)
        if text:
            return AIAnswer(text, "Ollama unavailable — answered by Gemini.", [f"gemini:{self.config.GEMINI_MODEL}"], evidence)
        return self._no_backend_response(evidence)

    def _no_backend_response(self, evidence: List[Dict[str, str]]) -> AIAnswer:
        return AIAnswer(
            text=(
                "No AI backend is available right now. "
                "Make sure Ollama is running locally, or set GEMINI_API_KEY / "
                "OPENAI_API_KEY in your environment variables."
            ),
            reasoning="No configured or reachable AI backend.",
            sources=["none"],
            evidence=evidence,
        )

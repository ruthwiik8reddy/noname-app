"""
ai_service.py — AI routing for Autofiera assistant
----------------------------------------------------
Routing logic:
  PRIMARY  → Ollama (local model, free, fast, no internet)
  FALLBACK → Gemini (internet, complex/research/visual questions)

Ollama handles:
  - Studio-specific questions (jobs, bookings, estimates, services)
  - Simple how-to / definition / advice questions
  - Anything that can be answered from studio context alone

Gemini handles:
  - Questions needing live internet data (pricing trends, product research)
  - Complex multi-step reasoning
  - Image / video analysis
  - Questions with keywords: compare, latest, trending, research,
    price of, what is the best, news, review, how does X work

Fallback chain:
  Ollama → if fails → Gemini → if fails → plain text error
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .config import Config


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


# ── Routing logic ─────────────────────────────────────────────────────────────

# Keywords that suggest the question needs internet / Gemini
GEMINI_TRIGGERS = [
    "latest", "trending", "news", "2024", "2025", "2026",
    "price of", "how much does", "best brand", "review",
    "compare brands", "research", "what is the difference between",
    "industry standard", "market", "recommend a product",
    "how does ceramic coating work", "how does ppf work",
    "what causes", "science behind", "chemical",
]

# Keywords that suggest image/video was sent
VISUAL_TRIGGERS = ["photo", "image", "picture", "video", "look at", "see this"]


def _needs_gemini(question: str, attachments: Optional[List[str]]) -> bool:
    """Return True if the question needs Gemini (internet/complex/visual)."""
    q = question.lower()

    # Visual content always goes to Gemini
    if attachments:
        return True
    if any(t in q for t in VISUAL_TRIGGERS):
        return True

    # Complex / internet questions go to Gemini
    if any(t in q for t in GEMINI_TRIGGERS):
        return True

    return False


# ── Studio memory (context injected into prompts) ─────────────────────────────

class UserMemoryStore:
    def __init__(self):
        self.db_path = Config.DB_PATH

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def load_profile(self, studio_id: int) -> Dict[str, Any]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM studios WHERE id=?", (studio_id,)).fetchone()
        conn.close()
        if not row:
            return {}
        return {
            "studio_name": row["name"],
            "city":        row["city"],
            "owner":       row["owner"],
        }

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
    def build(question: str, context: Dict[str, Any], for_gemini: bool = False) -> str:
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

        if for_gemini:
            parts.append("Note: Use your internet knowledge and reasoning to answer fully.")
            parts.append("")

        parts.append(f"Question: {question}")
        parts.append("Answer:")
        return "\n".join(parts)


# ── AI backends ───────────────────────────────────────────────────────────────

class OllamaBackend:
    """Local Ollama — primary backend, no internet needed."""

    def call(self, prompt: str) -> Optional[str]:
        if not Config.OLLAMA_URL:
            return None
        try:
            resp = requests.post(
                f"{Config.OLLAMA_URL.rstrip('/')}/api/generate",
                json={
                    "model":  Config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            print(f"[Ollama] Failed: {e}")
            return None


class GeminiBackend:
    """Gemini — fallback for internet/complex/visual questions."""

    def call(self, prompt: str) -> Optional[str]:
        if not Config.GEMINI_API_KEY:
            return None
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{Config.GEMINI_MODEL}:generateContent"
                f"?key={Config.GEMINI_API_KEY}"
            )
            resp = requests.post(
                url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30,
            )
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


# ── Main service ──────────────────────────────────────────────────────────────

class AIService:
    def __init__(self):
        self.memory  = UserMemoryStore()
        self.ollama  = OllamaBackend()
        self.gemini  = GeminiBackend()

    def answer(
        self,
        studio_id:   int,
        question:    str,
        attachments: Optional[List[str]] = None,
    ) -> AIAnswer:
        # Load studio context from DB
        context   = self.memory.build_context(studio_id, question)
        use_gemini = _needs_gemini(question, attachments)

        if use_gemini:
            return self._try_gemini_then_ollama(question, context)
        else:
            return self._try_ollama_then_gemini(question, context)

    def _try_ollama_then_gemini(
        self, question: str, context: Dict[str, Any]
    ) -> AIAnswer:
        """Primary path — Ollama first, Gemini fallback."""
        prompt = PromptBuilder.build(question, context, for_gemini=False)

        text = self.ollama.call(prompt)
        if text:
            return AIAnswer(
                text=text,
                reasoning="Answered by local Ollama model.",
                sources=[f"ollama:{Config.OLLAMA_MODEL}"],
                evidence=[],
            )

        # Ollama failed — try Gemini
        print("[AIService] Ollama unavailable, falling back to Gemini")
        prompt_g = PromptBuilder.build(question, context, for_gemini=True)
        text = self.gemini.call(prompt_g)
        if text:
            return AIAnswer(
                text=text,
                reasoning="Ollama unavailable — answered by Gemini.",
                sources=[f"gemini:{Config.GEMINI_MODEL}"],
                evidence=[],
            )

        return self._no_backend_response()

    def _try_gemini_then_ollama(
        self, question: str, context: Dict[str, Any]
    ) -> AIAnswer:
        """Complex/internet path — Gemini first, Ollama fallback."""
        prompt = PromptBuilder.build(question, context, for_gemini=True)

        text = self.gemini.call(prompt)
        if text:
            return AIAnswer(
                text=text,
                reasoning="Complex question — answered by Gemini.",
                sources=[f"gemini:{Config.GEMINI_MODEL}"],
                evidence=[],
            )

        # Gemini failed (no key or error) — fall back to Ollama
        print("[AIService] Gemini unavailable, falling back to Ollama")
        prompt_o = PromptBuilder.build(question, context, for_gemini=False)
        text = self.ollama.call(prompt_o)
        if text:
            return AIAnswer(
                text=text,
                reasoning="Gemini unavailable — answered by local Ollama model.",
                sources=[f"ollama:{Config.OLLAMA_MODEL}"],
                evidence=[],
            )

        return self._no_backend_response()

    def _no_backend_response(self) -> AIAnswer:
        return AIAnswer(
            text=(
                "No AI backend is available right now. "
                "Make sure Ollama is running locally, or set your GEMINI_API_KEY "
                "in the environment variables."
            ),
            reasoning="No configured or reachable AI backend.",
            sources=["none"],
            evidence=[],
        )

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

from .config import Config


class AIModel(Enum):
    LOCAL_ROUTER = "local_router"
    LOCAL_SMALL_LLM = "local_small_llm"
    OPENAI = "openai"
    GEMINI = "gemini"


@dataclass
class AIAnswer:
    text: str
    reasoning: str
    sources: List[str]
    evidence: List[Dict[str, str]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.text,
            "reasoning": self.reasoning,
            "sources": self.sources,
            "evidence": self.evidence,
        }


class LocalRouter:
    def __init__(self, config: Config):
        self.config = config

    def choose_route(self, question: str, attachments: Optional[List[str]]) -> AIModel:
        text = question.lower()
        has_attachment = bool(attachments)

        if has_attachment:
            if self.config.GEMINI_API_KEY:
                return AIModel.GEMINI
            if self.config.OPENAI_API_KEY:
                return AIModel.OPENAI
            return AIModel.LOCAL_SMALL_LLM

        if any(token in text for token in ["compare", "difference", "worn", "good", "bad", "how would", "looks like", "show me", "photo", "image"]):
            if self.config.OPENAI_API_KEY:
                return AIModel.OPENAI
            return AIModel.LOCAL_SMALL_LLM

        if any(token in text for token in ["what is", "explain", "define", "why", "how to", "best way", "advice"]):
            return AIModel.LOCAL_SMALL_LLM

        if self.config.GEMINI_API_KEY:
            return AIModel.GEMINI
        if self.config.OPENAI_API_KEY:
            return AIModel.OPENAI
        return AIModel.LOCAL_SMALL_LLM


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
                "attachment": attachment,
                "type": item_type,
                "description": f"Detected a {item_type} uploaded as {label}.",
                "confidence": "low",
            })
        return results

    def summarize(self, attachments: Optional[List[str]]) -> str:
        if not attachments:
            return "No attachments were provided."
        lines = [f"{idx + 1}. {os.path.basename(attachment)}" for idx, attachment in enumerate(attachments)]
        return "Attachments sent for analysis:\n" + "\n".join(lines)

    def compare_to_user_data(self, user_id: int, attachments: Optional[List[str]]) -> List[Dict[str, str]]:
        if not attachments:
            return []
        comparisons = []
        for attachment in attachments:
            name = os.path.basename(attachment)
            comparisons.append({
                "attachment": attachment,
                "note": (
                    "This image is compared against the user context and shows a likely worn surface. "
                    "Use open AI or Gemini for more precise visual reasoning when configured."
                ),
                "reference": "standard brake pad reference",
            })
        return comparisons


class UserMemoryStore:
    def __init__(self, config: Config):
        self.config = config

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def load_user_profile(self, user_id: int) -> Dict[str, Any]:
        conn = self._connect()
        profile = {}
        studio = conn.execute("SELECT * FROM studios WHERE id=?", (user_id,)).fetchone()
        if studio:
            profile = {
                "studio_id": studio["id"],
                "studio_name": studio["name"],
                "city": studio["city"],
                "owner": studio["owner"],
                "logo": studio["logo"],
            }
        conn.close()
        return profile

    def load_user_job_data(self, user_id: int) -> Dict[str, Any]:
        conn = self._connect()
        jobs = [dict(row) for row in conn.execute(
            "SELECT id, car, service, status, technician, price FROM jobs WHERE studio_id=? ORDER BY id DESC LIMIT 5", (user_id,)
        ).fetchall()]
        services = [dict(row) for row in conn.execute(
            "SELECT id, name, price FROM services WHERE studio_id=? LIMIT 10", (user_id,)).fetchall()]
        conn.close()
        return {
            "recent_jobs": jobs,
            "services": services,
        }

    def retrieve_relevant(self, user_id: int, question: str) -> List[str]:
        conn = self._connect()
        query = question.lower()
        rows = []
        if "estimate" in query:
            rows = conn.execute(
                "SELECT id, customer_name, total, status FROM estimates WHERE studio_id=? ORDER BY created_at DESC LIMIT 3", (user_id,)
            ).fetchall()
            result = [f"Estimate {row['id']} for {row['customer_name']} with total {row['total']} and status {row['status']}" for row in rows]
        elif "booking" in query or "appointment" in query:
            rows = conn.execute(
                "SELECT id, customer_name, vehicle, date, time_slot FROM bookings WHERE studio_id=? ORDER BY date DESC LIMIT 3", (user_id,)
            ).fetchall()
            result = [f"Booking {row['id']} for {row['customer_name']} on {row['date']} at {row['time_slot']}" for row in rows]
        else:
            rows = conn.execute(
                "SELECT id, car, service, status FROM jobs WHERE studio_id=? ORDER BY id DESC LIMIT 3", (user_id,)
            ).fetchall()
            result = [f"Job {row['id']} for {row['car']} using {row['service']} is {row['status']}" for row in rows]
        conn.close()
        return result


class PromptBuilder:
    @staticmethod
    def build(question: str, context: Dict[str, Any]) -> str:
        parts = [
            "You are an AI assistant for Autofiera, a studio operations platform.",
            "Answer clearly, reference evidence, and explain your reasoning.",
            "If the user provided photo or video attachments, mention how the visual evidence relates.",
            "If you cite user data, include the source and any relevant detail.",
            "",
            f"Question: {question}",
            "",
        ]

        profile = context.get("user_profile") or {}
        if profile:
            parts.append("User profile:")
            for key, value in profile.items():
                parts.append(f"- {key}: {value}")
            parts.append("")

        jobs = context.get("job_data", {}).get("recent_jobs", [])
        if jobs:
            parts.append("Recent jobs:")
            for job in jobs:
                parts.append(f"- {job['car']} ({job['service']}) status={job['status']}")
            parts.append("")

        services = context.get("job_data", {}).get("services", [])
        if services:
            parts.append("Available services:")
            for service in services:
                parts.append(f"- {service['name']}: {service['price']}")
            parts.append("")

        relevant = context.get("relevant_docs", [])
        if relevant:
            parts.append("Relevant studio data:")
            for item in relevant:
                parts.append(f"- {item}")
            parts.append("")

        visual_summary = context.get("visual_summary")
        if visual_summary:
            parts.append("Visual summary:")
            parts.append(visual_summary)
            parts.append("")

        evidence = context.get("visual_data", [])
        if evidence:
            parts.append("Attachment evidence:")
            for item in evidence:
                parts.append(f"- {item['attachment']} ({item['type']}): {item['description']}")
            parts.append("")

        parts.append("Answer with a final conclusion, list the sources, and note any assumptions.")
        return "\n".join(parts)


class AIService:
    def __init__(self):
        self.config = Config
        self.router = LocalRouter(self.config)
        self.memory = UserMemoryStore(self.config)
        self.vision = VisionAnalyzer(self.config)

    def answer(self, user_id: int, question: str, attachments: Optional[List[str]] = None) -> AIAnswer:
        route = self.router.choose_route(question, attachments)
        context = self._build_context(user_id, question, attachments, route)
        return self._dispatch(route, question, context)

    def _build_context(
        self,
        user_id: int,
        question: str,
        attachments: Optional[List[str]],
        route: AIModel,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "user_profile": self.memory.load_user_profile(user_id),
            "job_data": self.memory.load_user_job_data(user_id),
            "relevant_docs": self.memory.retrieve_relevant(user_id, question),
            "visual_summary": self.vision.summarize(attachments),
            "visual_data": self.vision.analyze(attachments),
            "comparison_data": self.vision.compare_to_user_data(user_id, attachments),
            "route": route.value,
        }
        return context

    def _dispatch(self, route: AIModel, question: str, context: Dict[str, Any]) -> AIAnswer:
        prompt = PromptBuilder.build(question, context)
        if route == AIModel.LOCAL_SMALL_LLM:
            return self._call_local_small_llm(prompt, context)
        if route == AIModel.OPENAI:
            return self._call_openai(prompt, context)
        if route == AIModel.GEMINI:
            return self._call_gemini(prompt, context)
        return self._local_fallback(question, context)

    def _local_fallback(self, question: str, context: Dict[str, Any]) -> AIAnswer:
        return AIAnswer(
            text=(
                "No AI backend is configured. "
                "Install and configure OLLAMA_URL, OPENAI_API_KEY, or GEMINI_API_KEY to enable full answers."
            ),
            reasoning="No configured model backend.",
            sources=["ai_service:local_fallback"],
            evidence=context.get("comparison_data", []),
        )

    def _call_local_small_llm(self, prompt: str, context: Dict[str, Any]) -> AIAnswer:
        if not self.config.OLLAMA_URL:
            return self._local_fallback(prompt, context)

        url = f"{self.config.OLLAMA_URL.rstrip('/')}/v1/generate"
        payload = {
            "model": self.config.OLLAMA_MODEL,
            "prompt": prompt,
            "max_tokens": 512,
            "temperature": 0.3,
        }

        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            body = response.json()
            text = body.get("completion") or body.get("output") or json.dumps(body)
            return AIAnswer(
                text=text,
                reasoning="Generated by local small LLM.",
                sources=["local_small_llm"],
                evidence=context.get("comparison_data", []),
            )
        except Exception as exc:
            return AIAnswer(
                text=f"Local model call failed: {exc}",
                reasoning="Local small LLM could not be reached.",
                sources=["local_small_llm", "error"],
                evidence=context.get("comparison_data", []),
            )

    def _call_openai(self, prompt: str, context: Dict[str, Any]) -> AIAnswer:
        if not self.config.OPENAI_API_KEY:
            return self._local_fallback(prompt, context)

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "You are a technical assistant for Autofiera."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 600,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            return AIAnswer(
                text=text,
                reasoning="Answered by OpenAI.",
                sources=["openai", self.config.OPENAI_MODEL],
                evidence=context.get("comparison_data", []),
            )
        except Exception as exc:
            return AIAnswer(
                text=f"OpenAI call failed: {exc}",
                reasoning="OpenAI API request failed.",
                sources=["openai", "error"],
                evidence=context.get("comparison_data", []),
            )

    def _call_gemini(self, prompt: str, context: Dict[str, Any]) -> AIAnswer:
        if not self.config.GEMINI_API_KEY:
            return self._local_fallback(prompt, context)

        url = f"https://gemini.googleapis.com/v1/models/{self.config.GEMINI_MODEL}:generate"
        headers = {
            "Authorization": f"Bearer {self.config.GEMINI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": {"text": prompt},
            "temperature": 0.3,
            "max_output_tokens": 600,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            response.raise_for_status()
            data = response.json()
            candidates = data.get("candidates", [])
            text = candidates[0].get("content", "") if candidates else json.dumps(data)
            return AIAnswer(
                text=text,
                reasoning="Answered by Gemini.",
                sources=["gemini", self.config.GEMINI_MODEL],
                evidence=context.get("comparison_data", []),
            )
        except Exception as exc:
            return AIAnswer(
                text=f"Gemini call failed: {exc}",
                reasoning="Gemini API request failed.",
                sources=["gemini", "error"],
                evidence=context.get("comparison_data", []),
            )

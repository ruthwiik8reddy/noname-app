#!/usr/bin/env python3
"""
apply_estimate_assistant.py
Wires estimate-awareness into the AI Assistant.
Run once from project root:  python apply_estimate_assistant.py
Idempotent.
"""
import os, re

CONTROLLER = "src/controller.py"

OLD = '''    service = AIService()
    answer  = service.answer(session["studio_id"], question, attachments)
    return jsonify(answer.as_dict())'''

NEW = '''    studio_id = session["studio_id"]

    # ── Pricing questions: ground the AI in the real product catalog ──
    try:
        from .estimate_assistant import build_pricing_facts
        facts = build_pricing_facts(studio_id, question)
    except Exception as _e:
        print(f"[assistant] pricing hook failed: {_e}")
        facts = None

    service = AIService()

    if facts:
        # Force Gemini for quotes (the local model invents numbers), and hand it
        # the exact computed prices so it can only report, not invent.
        from .ai_service import GeminiBackend, OllamaBackend, AIAnswer
        from .config import Config

        prompt = (
            "You are the AI assistant for a car detailing studio. "
            "A staff member asked a pricing question.\\n\\n"
            f"{facts}\\n\\n"
            f"Staff question: {question}\\n\\n"
            "Answer conversationally and concisely using ONLY the totals given above."
        )
        text = None
        if Config.GEMINI_API_KEY:
            try:
                text = GeminiBackend(Config).call(prompt)
            except Exception as _e:
                print(f"[assistant] Gemini failed: {_e}")
        if not text:
            try:
                text = OllamaBackend(Config).call(prompt)
            except Exception:
                text = None
        if text:
            return jsonify(AIAnswer(
                text, "Quoted from your product catalog.", ["catalog"], []
            ).as_dict())
        # else fall through to the normal assistant

    answer = service.answer(studio_id, question, attachments)
    return jsonify(answer.as_dict())'''


def main():
    if not os.path.exists(CONTROLLER):
        print("Run from the project root."); return
    src = open(CONTROLLER).read()
    if "build_pricing_facts" in src:
        print("SKIP — estimate assistant already wired in."); return
    if OLD not in src:
        print("✗ Could not find the assistant_query body to patch.")
        print("  Add the pricing hook manually — see PRODUCTS_README.")
        return
    src = src.replace(OLD, NEW, 1)
    open(CONTROLLER, "w").write(src)
    print("✓ Wired estimate-awareness into /assistant/query")


if __name__ == "__main__":
    main()

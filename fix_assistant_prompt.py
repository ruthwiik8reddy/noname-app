#!/usr/bin/env python3
"""
fix_assistant_prompt.py
Makes the AI Assistant give SHORT, direct price answers (no breakdown).
Use this if you ALREADY ran apply_estimate_assistant.py.
Run from project root:  python fix_assistant_prompt.py
"""
import os

CONTROLLER = "src/controller.py"

OLD = '"Answer conversationally and concisely using ONLY the totals given above."'
NEW = ('"Reply with the PRICE only — 1 or 2 short sentences. No cost breakdown, "\n'
       '            "no material/labour/markup figures, no calculation steps, no disclaimers, "\n'
       '            "no extra advice. Just the vehicle, the product, and the total."')

def main():
    if not os.path.exists(CONTROLLER):
        print("Run from the project root."); return
    src = open(CONTROLLER).read()
    if "Reply with the PRICE only" in src:
        print("SKIP — already using the concise prompt."); return
    if OLD not in src:
        print("✗ Couldn't find the prompt line in controller.py.")
        print("  Open src/controller.py, find this line inside assistant_query:")
        print('    "Answer conversationally and concisely using ONLY the totals given above."')
        print("  and replace it with a concise instruction.")
        return
    src = src.replace(OLD, NEW, 1)
    open(CONTROLLER, "w").write(src)
    print("✓ Assistant will now give short, direct price answers.")

if __name__ == "__main__":
    main()

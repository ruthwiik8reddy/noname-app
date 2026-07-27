#!/usr/bin/env python3
"""
unify_sidebars.py
-------------------
Run ONCE from project root: python unify_sidebars.py

Replaces the ENTIRE <aside class="sidebar">...</aside> block in every
template with a single {% include "_sidebar.html" %}. This eliminates
all per-page nav inconsistencies permanently — there is now exactly ONE
place (templates/_sidebar.html) that defines the sidebar.

Safe to re-run.
"""
import os
import re

TEMPLATES_DIR = "templates"

FILES = [
    "dashboard.html", "bookings.html", "new_booking.html",
    "estimates.html", "new_estimate.html", "estimate_detail.html",
    "jobs.html", "staff.html", "new_staff.html",
    "media.html", "upload_media.html",
    "customers.html", "new_customer.html", "customer_detail.html",
    "reminders.html", "new_reminder.html",
]

ASIDE_RE = re.compile(r'<aside class="sidebar">.*?</aside>', re.DOTALL)


def fix_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if '{% include "_sidebar.html" %}' in content:
        return "SKIP (already unified)"

    match = ASIDE_RE.search(content)
    if not match:
        return "WARN (no <aside class=\"sidebar\"> found)"

    new_content = content[: match.start()] + '{% include "_sidebar.html" %}' + content[match.end():]

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return "FIXED"


def main():
    if not os.path.isdir(TEMPLATES_DIR):
        print(f"Error: {TEMPLATES_DIR}/ not found. Run from project root.")
        return

    if not os.path.exists(os.path.join(TEMPLATES_DIR, "_sidebar.html")):
        print("Error: templates/_sidebar.html not found. Add it first.")
        return

    print(f"{'FILE':30s} RESULT")
    print("-" * 50)
    for filename in FILES:
        path = os.path.join(TEMPLATES_DIR, filename)
        if not os.path.exists(path):
            print(f"{filename:30s} MISSING FILE (skipped)")
            continue
        result = fix_file(path)
        print(f"{filename:30s} {result}")

    print()
    print("Done. Every page now uses templates/_sidebar.html as the single")
    print("source of truth for navigation.")


if __name__ == "__main__":
    main()

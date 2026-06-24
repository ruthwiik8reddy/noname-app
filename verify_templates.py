"""
verify_templates.py
----------------------
Run from project root: python verify_templates.py

Checks every template's <nav> block and reports whether it's using
nav_items (role-aware) or still has the old hardcoded links.
"""
import os
import re

TEMPLATES_DIR = "templates"
FILES_TO_CHECK = [
    "dashboard.html", "bookings.html", "new_booking.html",
    "estimates.html", "new_estimate.html", "estimate_detail.html",
    "jobs.html", "staff.html", "new_staff.html",
    "media.html", "upload_media.html",
    "customers.html", "new_customer.html", "customer_detail.html",
    "reminders.html", "new_reminder.html", "assistant.html",
]

print(f"{'FILE':30s} {'STATUS':20s} {'NAV ITEMS USED'}")
print("-" * 80)

for filename in FILES_TO_CHECK:
    path = os.path.join(TEMPLATES_DIR, filename)
    if not os.path.exists(path):
        print(f"{filename:30s} {'MISSING FILE':20s}")
        continue

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    nav_match = re.search(r"<nav>.*?</nav>", content, re.DOTALL)
    if not nav_match:
        print(f"{filename:30s} {'NO <nav> FOUND':20s}")
        continue

    nav_block = nav_match.group(0)
    is_role_aware = "nav_items" in nav_block

    if is_role_aware:
        found_keys = re.findall(r'"(\w+)" in nav_items', nav_block)
        print(f"{filename:30s} {'ROLE-AWARE':20s} {found_keys}")
    else:
        hardcoded_links = re.findall(r'href="([^"]+)"', nav_block)
        print(f"{filename:30s} {'HARDCODED (BUG)':20s} {hardcoded_links}")

print()
print("If any file shows HARDCODED, that file still has the old static nav.")

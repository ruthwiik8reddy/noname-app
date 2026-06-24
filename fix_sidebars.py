#!/usr/bin/env python3
"""
fix_sidebars.py
-----------------
Run ONCE from project root: python fix_sidebars.py

Replaces the static <nav>...</nav> block in every template with the
role-aware version that uses nav_items. Safe to re-run — it detects
templates that are already fixed and skips them.

It also tries to preserve which link should be "active" by checking
which template file it's editing.
"""
import os
import re

TEMPLATES_DIR = "templates"

# Maps template filename -> which nav key should get class="active"
ACTIVE_MAP = {
    "bookings.html":        "bookings",
    "new_booking.html":     "bookings",
    "estimates.html":       "estimates",
    "new_estimate.html":    "estimates",
    "estimate_detail.html": "estimates",
    "staff.html":           "staff",
    "new_staff.html":       "staff",
    "media.html":           "media",
    "upload_media.html":    "media",
    "customers.html":       "customers",
    "new_customer.html":    "customers",
    "customer_detail.html": "customers",
    "jobs.html":             "jobs",
}

NAV_LINKS = [
    ("dashboard",  "/dashboard",  "Dashboard"),
    ("bookings",   "/bookings",   "Bookings"),
    ("estimates",  "/estimates",  "Estimates"),
    ("jobs",       "/jobs",       "Jobs"),
    ("customers",  "/customers",  "Customers"),
    ("media",      "/media",      "Media"),
    ("staff",      "/staff",      "Staff"),
]

EXTRA_LINKS = """    <a href="/reminders">Reminders</a>
    <a href="/assistant">AI Assistant</a>"""


def build_nav(active_key: str) -> str:
    lines = ["  <nav>"]
    for key, href, label in NAV_LINKS:
        active_attr = ' class="active"' if key == active_key else ""
        lines.append(
            f'    {{% if "{key}" in nav_items %}}<a href="{href}"{active_attr}>{label}</a>{{% endif %}}'
        )
    lines.append(EXTRA_LINKS)
    lines.append("  </nav>")
    return "\n".join(lines)


NAV_BLOCK_RE = re.compile(r"  <nav>.*?</nav>", re.DOTALL)


def fix_file(path: str, active_key: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if "nav_items" in content:
        print(f"  SKIP  {path} (already role-aware)")
        return False

    match = NAV_BLOCK_RE.search(content)
    if not match:
        print(f"  WARN  {path} — no <nav> block found, skipped")
        return False

    new_nav = build_nav(active_key)
    new_content = content[: match.start()] + new_nav + content[match.end():]

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  FIXED {path} (active: {active_key})")
    return True


def main():
    if not os.path.isdir(TEMPLATES_DIR):
        print(f"Error: {TEMPLATES_DIR}/ not found. Run this from the project root.")
        return

    fixed = 0
    for filename, active_key in ACTIVE_MAP.items():
        path = os.path.join(TEMPLATES_DIR, filename)
        if os.path.exists(path):
            if fix_file(path, active_key):
                fixed += 1
        else:
            print(f"  MISS  {path} (file not found, skipped)")

    print()
    print(f"Done. {fixed} file(s) updated.")


if __name__ == "__main__":
    main()

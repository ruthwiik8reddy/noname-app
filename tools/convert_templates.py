#!/usr/bin/env python3
"""
tools/convert_templates.py — migrate legacy standalone templates onto _base.html.

Each legacy template is a full HTML document carrying its own <head>, its own
dark-theme <style> block, and its own copy of the page chrome. Once _sidebar.html
was rewritten against the new design system, those pages rendered the sidebar
with no matching CSS — hence the giant logo bleeding across the page.

This does the mechanical 90%: strips the document shell and the dead <style>
block, lifts the content into blocks, and maps the old class vocabulary onto
bayq.css. Anything it can't confidently place is left alone and reported, so a
human looks at it rather than the script guessing.
"""

from __future__ import annotations

import os
import re
import sys

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# Old class vocabulary → design-system equivalents.
CLASS_MAP = [
    (r'class="top-bar"', 'class="page-head"'),
    (r'class="page-heading"', 'class="page-title"'),
    (r'class="page-title-sub"', 'class="page-sub"'),
    (r'class="btn"', 'class="btn btn-primary"'),
    (r'class="btn btn-secondary btn-primary"', 'class="btn btn-secondary"'),
    (r'class="btn-secondary"', 'class="btn btn-secondary"'),
    (r'class="badge badge-pending"', 'class="badge status-pending"'),
    (r'class="badge badge-confirmed"', 'class="badge badge-violet"'),
    (r'class="badge badge-completed"', 'class="badge status-completed"'),
    (r'class="badge badge-cancelled"', 'class="badge status-cancelled"'),
    (r'class="badge badge-progress"', 'class="badge status-in-progress"'),
    (r'class="badge badge-draft"', 'class="badge status-draft"'),
    (r'class="badge badge-sent"', 'class="badge badge-blue"'),
    (r'class="badge badge-approved"', 'class="badge status-completed"'),
    (r'class="badge badge-declined"', 'class="badge badge-red"'),
    (r'class="stat-card"', 'class="card stat"'),
    (r'class="stat-label"', 'class="label"'),
    (r'class="stat-value"', 'class="value"'),
    (r'class="empty-state"', 'class="empty"'),
    (r'class="section-label"', 'class="section-title"'),
    (r'class="form-card"', 'class="card card-pad"'),
    (r'class="form-group"', 'class="field"'),
    (r'<table>', '<table class="data">'),
]


def convert(path: str) -> tuple[bool, str]:
    src = open(path, encoding="utf-8").read()

    if '{% extends' in src:
        return False, "already converted"
    if '_sidebar.html' not in src:
        return False, "no sidebar — not a shell page"

    title_m = re.search(r'<title>(.*?)</title>', src, re.S)
    title = title_m.group(1).strip() if title_m else "BayQ"

    # Everything after the sidebar include is page content.
    inc = re.search(r'\{%\s*include\s+"_sidebar\.html"\s*%\}', src)
    if not inc:
        return False, "sidebar include not found"
    body = src[inc.end():]

    # Drop the closing document tags.
    body = re.sub(r'</body>\s*</html>\s*$', '', body.strip(), flags=re.S).strip()

    # Lift <script> blocks into their own Jinja block.
    scripts = re.findall(r'<script\b[^>]*>.*?</script>', body, re.S)
    for s in scripts:
        body = body.replace(s, '')

    # Unwrap the <main> element — _base.html supplies it.
    m = re.search(r'<main[^>]*>(.*)</main>', body, re.S)
    if m:
        body = m.group(1)

    body = body.strip()
    for pattern, repl in CLASS_MAP:
        body = re.sub(pattern, repl, body)

    out = [
        '{% extends "_base.html" %}',
        '{% block title %}' + title + '{% endblock %}',
        '',
        '{% block content %}',
        body,
        '{% endblock %}',
    ]
    if scripts:
        out += ['', '{% block scripts %}', '\n'.join(s.strip() for s in scripts), '{% endblock %}']

    open(path, "w", encoding="utf-8").write('\n'.join(out) + '\n')
    return True, f"converted ({len(scripts)} script block(s))"


def main() -> None:
    targets = sys.argv[1:] or sorted(
        f for f in os.listdir(TEMPLATES)
        if f.endswith(".html") and not f.startswith("_")
    )
    done = skipped = 0
    for name in targets:
        path = os.path.join(TEMPLATES, name)
        if not os.path.exists(path):
            print(f"  ?? {name} — not found")
            continue
        ok, note = convert(path)
        print(f"  {'✓' if ok else '·'} {name:34} {note}")
        done += ok
        skipped += (not ok)
    print(f"\n{done} converted, {skipped} skipped")


if __name__ == "__main__":
    main()

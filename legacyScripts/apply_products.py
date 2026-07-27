#!/usr/bin/env python3
"""
apply_products.py — wires the Products catalog + AI estimator into the app.
Run once from project root:  python apply_products.py
Idempotent.
"""
import os, re

def patch_auth():
    """Add 'products' to admin nav (admin-only, as requested)."""
    path = "src/auth.py"
    src = open(path).read()
    if '"products"' in src:
        print("  SKIP auth.py — products nav already present"); return
    # add to the admin list only
    src = src.replace(
        '''    "admin": [
        "dashboard", "bookings", "estimates", "jobs",
        "tracking", "customers", "staff", "media", "payments",
        "warranties", "settings",
    ],''',
        '''    "admin": [
        "dashboard", "bookings", "estimates", "jobs",
        "tracking", "customers", "staff", "media", "payments",
        "products", "warranties", "settings",
    ],''')
    open(path, "w").write(src)
    print("  ✓ auth.py — added 'products' to admin nav")

def patch_sidebar():
    """Add Products + AI Estimate links to the shared sidebar."""
    path = "templates/_sidebar.html"
    if not os.path.exists(path):
        print("  ! templates/_sidebar.html not found — add links manually"); return
    src = open(path).read()
    if 'href="/products"' in src:
        print("  SKIP _sidebar.html — links already present"); return

    # Insert after the estimates link (keeps a sensible order)
    est = '{% if "estimates" in nav_items %}<a href="/estimates" {% if active_page == "estimates" %}class="active"{% endif %}>Estimates</a>{% endif %}'
    add = (est
           + '\n    {% if "estimates" in nav_items %}<a href="/ai-estimate" {% if active_page == "ai_estimate" %}class="active"{% endif %}>AI Estimate</a>{% endif %}'
           + '\n    {% if "products" in nav_items %}<a href="/products" {% if active_page == "products" %}class="active"{% endif %}>Products</a>{% endif %}')
    if est in src:
        src = src.replace(est, add, 1)
        open(path, "w").write(src)
        print("  ✓ _sidebar.html — added Products + AI Estimate links")
    else:
        print("  ! couldn't find the estimates nav line — add these manually to _sidebar.html:")
        print('    <a href="/ai-estimate">AI Estimate</a>')
        print('    <a href="/products">Products</a>')

def patch_controller():
    path = "src/controller.py"
    src = open(path).read()
    if "product_routes" in src:
        print("  SKIP controller.py — product_routes already wired"); return
    with open(path, "a") as f:
        f.write("\n\n# ── Products catalog + AI sqft estimator ──\n")
        f.write("from . import product_routes  # noqa: E402,F401\n")
    print("  ✓ controller.py — registered product_routes")

def main():
    if not os.path.exists("src/controller.py"):
        print("Run from the project root."); return
    print("Wiring Products + AI Estimator…")
    patch_auth()
    patch_sidebar()
    patch_controller()
    print("✓ done")

if __name__ == "__main__":
    main()

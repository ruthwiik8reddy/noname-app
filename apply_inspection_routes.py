#!/usr/bin/env python3
"""
apply_inspection_routes.py — wires the full inspection tracker into the app.
Run once from project root:  python apply_inspection_routes.py

It appends an import line to controller.py so the routes in
src/inspection_routes.py get registered on the blueprint.
Safe / idempotent.
"""
import os

CONTROLLER = "src/controller.py"
MARKER = "import src.inspection_routes"       # what we append
ALT_MARKER = "from . import inspection_routes"

def main():
    if not os.path.exists(CONTROLLER):
        print("Run from project root (src/controller.py not found)."); return
    src = open(CONTROLLER).read()
    if "inspection_routes" in src:
        print("SKIP — inspection_routes already wired in."); return
    # Append an import at the very end so all @bp.route decorators run.
    with open(CONTROLLER, "a") as f:
        f.write("\n\n# ── Full inspection tracker (video, SVG car, warranty gate, AI) ──\n")
        f.write("from . import inspection_routes  # noqa: E402,F401\n")
    print("✓ Wired inspection_routes into controller.py")

if __name__ == "__main__":
    main()

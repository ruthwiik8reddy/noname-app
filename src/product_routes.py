"""
product_routes.py — Products catalog + AI sqft-based estimator
---------------------------------------------------------------
Admin:
  /products                     list catalog
  /products/new                 add product (GET/POST)
  /products/<id>/edit           edit product (GET/POST)
  /products/<id>/delete         POST delete (soft)

Estimator (staff):
  /ai-estimate                  GET  the standalone estimator page
  /ai-estimate/sqft             POST → AI estimates sqft from vehicle name
  /ai-estimate/save             POST → saves the quote as an Estimate
"""
import json
import logging
import re
from flask import request, redirect, url_for, session, jsonify, render_template

logger = logging.getLogger(__name__)

from .db_manager import get_db
from .auth import staff_or_admin_required, admin_required
# Orchestrator Pattern: no model backends are imported here. Surface-area
# estimation lives in EstimateOrchestrator, which owns the cache → model →
# body-type-prior chain that used to be inlined in this module.
from .services.orchestrators import AssistantOrchestrator, EstimateOrchestrator

_estimate_ai = EstimateOrchestrator()
_assistant_ai = AssistantOrchestrator()
from .config import Config
from . import controller as _c

bp = _c.bp

CATEGORIES = ["PPF", "Ceramic", "Graphene", "Paint Correction", "Interior", "Other"]

# Fallback sqft by rough body type — used if the AI can't answer.
FALLBACK_SQFT = {
    "hatchback": 170, "sedan": 200, "coupe": 185,
    "suv": 250, "truck": 270, "van": 280, "default": 210,
}


# ═══════════ PRODUCTS CATALOG (admin) ═══════════

@bp.route("/products")
@admin_required
def products_list():
    logger.info("Entering products_list()")
    sid = session["studio_id"]; conn = get_db()
    rows = conn.execute(
        "SELECT * FROM products WHERE studio_id=? AND active=1 ORDER BY category, name",
        (sid,)
    ).fetchall()
    return render_template("products.html",
        **_c.sidebar_context(), **_c.auth_context(),
        products=rows, active_page="products")


@bp.route("/products/new", methods=["GET", "POST"])
@admin_required
def product_new():
    logger.info(f"Entering product_new(method={request.method})")
    sid = session["studio_id"]; conn = get_db()
    if request.method == "POST":
        try:
            f = request.form
            conn.execute("""
                INSERT INTO products
                  (studio_id, name, category, brand, cost_per_sqft, labour_per_sqft,
                   markup_percent, warranty_years, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (sid,
                  f.get("name", "").strip(),
                  f.get("category", "PPF"),
                  f.get("brand", "").strip(),
                  float(f.get("cost_per_sqft") or 0),
                  float(f.get("labour_per_sqft") or 0),
                  float(f.get("markup_percent") or 0),
                  int(f.get("warranty_years") or 0),
                  f.get("notes", "").strip()))
            conn.commit()
            logger.info("Exiting product_new — product created")
            return redirect(url_for("main.products_list"))
        except Exception as e:
            logger.error(f"Error in product_new: {e}", exc_info=True)
            return redirect(url_for("main.products_list"))
    return render_template("product_form.html",
        **_c.sidebar_context(), **_c.auth_context(),
        product=None, categories=CATEGORIES, active_page="products")


@bp.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def product_edit(pid):
    logger.info(f"Entering product_edit(pid={pid}, method={request.method})")
    sid = session["studio_id"]; conn = get_db()
    p = conn.execute("SELECT * FROM products WHERE id=? AND studio_id=?", (pid, sid)).fetchone()
    if not p:
        return "Product not found", 404
    if request.method == "POST":
        try:
            f = request.form
            conn.execute("""
                UPDATE products SET name=?, category=?, brand=?, cost_per_sqft=?,
                    labour_per_sqft=?, markup_percent=?, warranty_years=?, notes=?
                WHERE id=? AND studio_id=?
            """, (f.get("name","").strip(), f.get("category","PPF"), f.get("brand","").strip(),
                  float(f.get("cost_per_sqft") or 0), float(f.get("labour_per_sqft") or 0),
                  float(f.get("markup_percent") or 0), int(f.get("warranty_years") or 0),
                  f.get("notes","").strip(), pid, sid))
            conn.commit()
            logger.info(f"Exiting product_edit — product {pid} updated")
            return redirect(url_for("main.products_list"))
        except Exception as e:
            logger.error(f"Error in product_edit(pid={pid}): {e}", exc_info=True)
            return redirect(url_for("main.products_list"))
    return render_template("product_form.html",
        **_c.sidebar_context(), **_c.auth_context(),
        product=p, categories=CATEGORIES, active_page="products")


@bp.route("/products/<int:pid>/delete", methods=["POST"])
@admin_required
def product_delete(pid):
    logger.info(f"Entering product_delete(pid={pid})")
    try:
        sid = session["studio_id"]; conn = get_db()
        conn.execute("UPDATE products SET active=0 WHERE id=? AND studio_id=?", (pid, sid))
        conn.commit()
        logger.info(f"Exiting product_delete — product {pid} soft-deleted")
    except Exception as e:
        logger.error(f"Error in product_delete(pid={pid}): {e}", exc_info=True)
    return redirect(url_for("main.products_list"))


# ═══════════ AI SQFT ESTIMATOR ═══════════

def _parse_sqft(text: str):
    """Pull the first plausible sqft number out of a model reply."""
    if not text:
        return None
    cleaned = text.replace(",", "")
    # prefer a number that sits next to a sqft-ish word, else first number in range
    m = re.search(r"(\d{2,4}(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s*feet|ft2|ft\^?2)", cleaned, re.I)
    if not m:
        m = re.search(r"(\d{2,4}(?:\.\d+)?)", cleaned)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if 80 <= val <= 600 else None


def _ai_estimate_sqft(vehicle: str, studio_id: int):
    """
    Estimate a vehicle's wrappable surface area in square feet.

    Thin adapter kept for backwards compatibility — every caller in this module
    and in estimate_assistant.py still expects the `(sqft, source)` tuple. The
    logic itself (cache lookup, model call, plausibility check, body-type
    fallback, cache write) now lives in EstimateOrchestrator.

    `source` is one of: "cache" | "ai" | "fallback".
    """
    result = _estimate_ai.vehicle_sqft(vehicle, studio_id)
    return float(result["sqft"]), result["source"]


def _price(product, sqft, coverage=1.0):
    """Compute price for a product over a given sqft and coverage fraction."""
    area = sqft * coverage
    material = area * float(product["cost_per_sqft"] or 0)
    labour   = area * float(product["labour_per_sqft"] or 0)
    base     = material + labour
    markup   = base * (float(product["markup_percent"] or 0) / 100.0)
    return {
        "area": round(area, 1),
        "material": round(material, 2),
        "labour": round(labour, 2),
        "markup": round(markup, 2),
        "total": round(base + markup, 2),
    }


@bp.route("/ai-estimate")
@staff_or_admin_required
def ai_estimate_page():
    logger.info("Entering ai_estimate_page()")
    sid = session["studio_id"]; conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE studio_id=? AND active=1 ORDER BY category, name", (sid,)
    ).fetchall()
    return render_template("ai_estimate.html",
        **_c.sidebar_context(), **_c.auth_context(),
        products=products, active_page="estimates")


@bp.route("/ai-estimate/query", methods=["POST"])
@staff_or_admin_required
def ai_estimate_query():
    """
    Quote endpoint for the AI Estimate page.

    This page used to POST to /assistant/query. When the chat assistant was
    removed, that coupling would have silently broken quoting — so the
    estimator owns its own route now.

    Pricing questions are grounded: `build_pricing_facts` computes real figures
    from the product catalog in Python, and the model may only restate them.
    """
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Describe the vehicle and the work you need quoted."}), 400

    studio_id = session["studio_id"]
    try:
        from .estimate_assistant import build_pricing_facts
        facts = build_pricing_facts(studio_id, question)
    except Exception as exc:  # noqa: BLE001 - a broken hook must not break quoting
        logger.warning("Pricing hook failed: %s", exc)
        facts = None

    if facts:
        grounded = _assistant_ai.pricing_answer(question, facts)
        if grounded:
            return jsonify(grounded)

    return jsonify(_assistant_ai.answer(studio_id, question, []))


@bp.route("/ai-estimate/sqft", methods=["POST"])
@staff_or_admin_required
def ai_estimate_sqft():
    logger.info("Entering ai_estimate_sqft()")
    sid = session["studio_id"]
    data = request.get_json(silent=True) or {}
    vehicle = data.get("vehicle", "").strip()
    sqft, source = _ai_estimate_sqft(vehicle, sid)

    # price every active product at this sqft
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE studio_id=? AND active=1 ORDER BY category, name", (sid,)
    ).fetchall()
    coverage = float(data.get("coverage") or 1.0)
    quotes = []
    for p in products:
        q = _price(p, sqft, coverage)
        quotes.append({
            "id": p["id"], "name": p["name"], "category": p["category"],
            "brand": p["brand"], "warranty_years": p["warranty_years"],
            "cost_per_sqft": p["cost_per_sqft"], "labour_per_sqft": p["labour_per_sqft"],
            "markup_percent": p["markup_percent"], **q,
        })
    return jsonify({"vehicle": vehicle, "sqft": sqft, "source": source,
                    "coverage": coverage, "quotes": quotes})


@bp.route("/ai-estimate/save", methods=["POST"])
@staff_or_admin_required
def ai_estimate_save():
    """Save the chosen AI quote as a real Estimate + line item."""
    logger.info("Entering ai_estimate_save()")
    sid = session["studio_id"]; conn = get_db()
    try:
        f = request.form
        vehicle   = f.get("vehicle", "").strip()
        cust_name = f.get("customer_name", "").strip() or "Walk-in"
        pid       = int(f.get("product_id"))
        sqft      = float(f.get("sqft") or 0)
        coverage  = float(f.get("coverage") or 1.0)

        p = conn.execute("SELECT * FROM products WHERE id=? AND studio_id=?", (pid, sid)).fetchone()
        if not p:
            return "Product not found", 404
        q = _price(p, sqft, coverage)

        tax_pct = float(f.get("tax_percent") or 0)
        subtotal = q["total"]
        tax_amt  = round(subtotal * tax_pct / 100.0, 2)
        total    = round(subtotal + tax_amt, 2)

        cur = conn.execute("""
            INSERT INTO estimates
              (studio_id, customer_name, vehicle, status, subtotal, tax_percent, tax_amount, total, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (sid, cust_name, vehicle, "Draft", subtotal, tax_pct, tax_amt, total,
              f"AI estimate · {q['area']} sqft @ {p['name']}"))
        est_id = cur.lastrowid

        conn.execute("""
            INSERT INTO estimate_items (estimate_id, name, description, quantity, unit_price, total)
            VALUES (?,?,?,?,?,?)
        """, (est_id, p["name"],
              f"{q['area']} sqft · material ${q['material']} + labour ${q['labour']} + margin ${q['markup']}",
              q["area"],
              round(q["total"] / q["area"], 2) if q["area"] else 0,
              q["total"]))
        conn.commit()
        logger.info(f"Exiting ai_estimate_save — created estimate {est_id}")
        return redirect(url_for("main.estimate_detail", estimate_id=est_id))
    except Exception as e:
        logger.error(f"Error in ai_estimate_save: {e}", exc_info=True)
        return redirect(url_for("main.ai_estimate_page"))


@bp.route("/ai-estimate/debug")
@staff_or_admin_required
def ai_estimate_debug():
    """Diagnostics — tells you exactly why the estimator is/isn't using Gemini."""
    logger.info("Entering ai_estimate_debug()")
    info = {
        "gemini_key_set": bool(Config.GEMINI_API_KEY),
        "gemini_key_preview": (Config.GEMINI_API_KEY[:6] + "…") if Config.GEMINI_API_KEY else None,
        "gemini_model": Config.GEMINI_MODEL,
        "ollama_model": getattr(Config, "OLLAMA_MODEL", None),
    }
    # Live health check against the actual configured backend.
    from .services.llm.factory import LLMProviderFactory

    provider = LLMProviderFactory.text_provider()
    info["provider"] = provider.describe()
    try:
        probe = _estimate_ai.vehicle_sqft("2022 Toyota Camry", session["studio_id"])
        info["live_test"] = probe
    except Exception as e:  # noqa: BLE001
        info["live_test"] = {"error": str(e)}

    # what products exist (an empty catalog = no quotes shown!)
    sid = session["studio_id"]
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) c FROM products WHERE studio_id=? AND active=1", (sid,)).fetchone()["c"]
    info["active_products"] = n
    if n == 0:
        info["hint"] = "No products in the catalog — the estimator has nothing to price. Add one at /products/new."

    return jsonify(info)
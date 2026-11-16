"""
manage.py — server-side admin commands.

    python -m src.manage create-studio      add a studio and its owner login
    python -m src.manage reset-password     set a new password for any login
    python -m src.manage backup             consistent copy of the database (safe while running)
    python -m src.manage move-legacy-uploads  move old photos out of public static/ storage
    python -m src.manage check              is this server ready for production?

Passwords are typed at a hidden prompt, or read from stdin with
--password-stdin for scripts. They are never taken as command-line arguments,
which would leave them in shell history and the process list.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .config import Config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _read_password(args) -> str:
    from .auth import validate_password_strength
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords don't match.")
    problems = validate_password_strength(password)
    if problems:
        sys.exit("Password too weak: " + " ".join(problems))
    return password


# ── commands ──────────────────────────────────────────────────────────────────

def create_studio(args) -> None:
    from .auth import hash_password, username_taken
    from .seed import initialize_db
    from .services import currency
    from .services.repositories.settings_repository import SettingsRepository

    initialize_db()
    username = args.username.strip().lower()
    code = args.currency.upper()
    if not currency.valid(code):
        sys.exit(f"Unknown currency {code}. Choose one of: {', '.join(currency.CURRENCIES)}")
    with _connect() as conn:
        if username_taken(conn, username):
            sys.exit(f"The username '{username}' is already used.")
    password = _read_password(args)
    with _connect() as conn:
        cur = conn.execute("INSERT INTO studios (name, city, owner, logo, username, password) VALUES (?,?,?,?,?,?)",
                           (args.name.strip(), args.city.strip(), args.owner.strip(), args.logo or "",
                            username, hash_password(password)))
        studio_id = cur.lastrowid
    SettingsRepository().set(studio_id, currency.SETTING, code)
    if args.country_code:
        SettingsRepository().set(studio_id, "whatsapp_country_code", args.country_code.lstrip("+"))
    print(f"✓ Created studio #{studio_id} “{args.name}”. The owner signs in as '{username}'. Currency: {code}.")


def reset_password(args) -> None:
    from .auth import hash_password
    username = args.username.strip().lower()
    with _connect() as conn:
        table = next((t for t in ("studios", "staff", "customers")
                      if conn.execute(f"SELECT 1 FROM {t} WHERE LOWER(username)=?", (username,)).fetchone()), None)
    if not table:
        sys.exit(f"No login called '{username}'.")
    password = _read_password(args)
    with _connect() as conn:
        conn.execute(f"UPDATE {table} SET password=? WHERE LOWER(username)=?", (hash_password(password), username))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='login_throttle'").fetchone():
            conn.execute("DELETE FROM login_throttle WHERE username=?", (username,))   # lift any lockout
    print(f"✓ Password changed for '{username}'. Their other sessions end on their next page load.")


def backup(args) -> Path:
    """sqlite3's online backup: a consistent snapshot even while the app is writing."""
    dest_dir = Path(args.dest).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)                      # customer data: owner-only
    target = dest_dir / f"bayq-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(Config.DB_PATH, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        target.unlink(missing_ok=True)
        sys.exit(f"Backup failed its integrity check: {ok}")
    os.chmod(target, 0o600)
    if args.keep:
        old = sorted(dest_dir.glob("bayq-*.db"))[:-args.keep]
        for path in old:
            path.unlink()
    print(f"✓ Backup written: {target} ({target.stat().st_size // 1024} KB)")
    return target


def move_legacy_uploads(args) -> None:
    """Older versions saved customer photos under static/ (public). Move them to private storage."""
    source = Path(Config.STATIC_FOLDER) / "uploads"
    target = Path(Config.PRIVATE_UPLOAD_ROOT)
    files = [p for p in source.rglob("*") if p.is_file() and p.name != ".gitkeep"] if source.exists() else []
    moved = skipped = 0
    for path in files:
        dest = target / path.relative_to(source)
        if dest.exists():
            skipped += 1
            print(f"  skipped (already in private storage): {path.relative_to(source)}")
            continue
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
        moved += 1
    verb = "Would move" if args.dry_run else "Moved"
    print(f"✓ {verb} {moved} file(s) to {target}" + (f"; {skipped} left in place (same name exists)" if skipped else ""))


def check(_args) -> None:
    """Pass/fail list of what production needs. Exit code 1 if anything fails."""
    import requests

    results = []

    def item(ok: bool, label: str, hint: str = "") -> None:
        results.append(ok)
        print(("  ✓ " if ok else "  ✗ ") + label + ("" if ok or not hint else f"\n      → {hint}"))

    print("Configuration")
    try:
        Config.validate()
        item(True, "Production settings valid" if Config.PRODUCTION else "Settings valid (development mode)")
    except RuntimeError as exc:
        item(False, "Production settings", str(exc))
    item(Config.PRODUCTION, "APP_ENV=production", "Set APP_ENV=production in .env")
    item(not Config.SECRET_KEY_IS_EPHEMERAL, "SECRET_KEY set", "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\"")
    item(not Config.DEBUG, "Debug mode off", "Set FLASK_DEBUG=0")
    item(not Config.SEED_DEMO, "Demo data off", "Set SEED_DEMO=0")
    item(Config.TRUSTED_PROXIES > 0, "Reverse proxy trusted (TRUSTED_PROXIES)",
         "Set TRUSTED_PROXIES=1 when running behind nginx")

    print("Database")
    try:
        with _connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            studios = conn.execute("SELECT COUNT(*) FROM studios").fetchone()[0]
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        item(True, f"Reachable: {Config.DB_PATH}")
        item(mode == "wal", f"Write-ahead logging ({mode})", "Start the app once; it switches the database to WAL")
        item(integrity == "ok", "Integrity check", integrity)
        item(studios > 0, f"{studios} studio(s)", "Create one: python -m src.manage create-studio --help")
        item(os.access(os.path.dirname(os.path.abspath(Config.DB_PATH)), os.W_OK), "Database folder writable")
    except sqlite3.Error as exc:
        item(False, "Database", str(exc))
    free_gb = shutil.disk_usage(os.path.dirname(os.path.abspath(Config.DB_PATH))).free / 1e9
    item(free_gb > 2, f"Free disk {free_gb:.1f} GB", "Less than 2 GB left")

    print("Private uploads")
    root = Path(Config.PRIVATE_UPLOAD_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    item(os.access(root, os.W_OK), f"Writable: {root}")

    print("Local AI (optional — features fall back without it)")
    if str(Config.AI_ENABLED) == "0":
        item(True, "AI disabled (AI_ENABLED=0)")
    else:
        try:
            names = {m["name"] for m in requests.get(f"{Config.OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])}
            item(True, f"Ollama reachable at {Config.OLLAMA_URL}")
            for model in (Config.OLLAMA_MODEL, Config.OLLAMA_VISION_MODEL):
                present = model in names or f"{model}:latest" in names
                item(present, f"Model {model}", f"ollama pull {model}")
        except Exception as exc:  # noqa: BLE001
            item(False, f"Ollama at {Config.OLLAMA_URL}", f"Not reachable ({exc.__class__.__name__}). Start it or set AI_ENABLED=0")

    failed = results.count(False)
    print(f"\n{'Ready.' if not failed else f'{failed} item(s) need attention.'}")
    sys.exit(1 if failed else 0)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m src.manage", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-studio", help="add a studio and its owner login")
    p.add_argument("--name", required=True, help="studio name, e.g. 'Autofiera Detailing Studio'")
    p.add_argument("--city", required=True)
    p.add_argument("--owner", required=True, help="owner's full name")
    p.add_argument("--username", required=True, help="owner's login name")
    p.add_argument("--currency", default="USD", help="INR, USD, EUR… (default USD)")
    p.add_argument("--country-code", default="", help="WhatsApp dialling code, e.g. 91")
    p.add_argument("--logo", default="", help="logo file name in static/logos (optional)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=create_studio)

    p = sub.add_parser("reset-password", help="set a new password for an owner, staff or customer login")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=reset_password)

    p = sub.add_parser("backup", help="write a consistent copy of the database")
    p.add_argument("--dest", default=os.path.join(os.path.dirname(os.path.abspath(Config.DB_PATH)), "backups"))
    p.add_argument("--keep", type=int, default=14, help="how many backups to keep (0 = all)")
    p.set_defaults(func=backup)

    p = sub.add_parser("move-legacy-uploads", help="move old customer photos out of static/ into private storage")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=move_legacy_uploads)

    p = sub.add_parser("check", help="production readiness checklist")
    p.set_defaults(func=check)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

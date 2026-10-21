"""
schema_repair.py — bring an older database up to the current schema.

Several early migrations were one-off scripts (run by hand, hard-wired to
studios.db) and never ran at startup. Databases created before them are
missing columns the code now relies on — e.g. bookings.estimate_id, which
made "New booking" fail with "table bookings has no column named estimate_id".

Rather than chase those scripts one by one, this builds what a brand-new
database looks like today (in a temporary file), compares it with the real
one, and adds any missing column. It only ever ADDs columns: nothing is
dropped, renamed or rewritten, and existing rows keep their data.

SQLite limits what ALTER TABLE ADD COLUMN accepts, so a column is added
without NOT NULL when it has no constant default, and without an expression
default such as (datetime('now')).

It also relaxes columns an old database still marks as required (NOT NULL)
when the current code treats them as optional — e.g. customers.email,
.username and .password from the retired customer-login era, which made
"New booking" fail once it creates a customer from just a name and phone.
SQLite can't alter a column's constraints, so that table is rebuilt (the
standard create-copy-swap procedure) after the database file is backed up.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import shutil
import tempfile
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def _columns(conn: sqlite3.Connection, table: str) -> Dict[str, tuple]:
    return {row[1]: row for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]


def _column_ddl(info: tuple) -> str:
    """ALTER TABLE-safe definition for a column from PRAGMA table_info."""
    _cid, name, col_type, notnull, default, _pk = info
    ddl = f'"{name}" {col_type or ""}'.strip()
    constant_default = default is not None and "(" not in str(default)
    if constant_default:
        ddl += f" DEFAULT {default}"
    if notnull and constant_default:
        ddl += " NOT NULL"
    return ddl


def reference_schema() -> Dict[str, Dict[str, tuple]]:
    """Every table and column a brand-new database gets today."""
    from .config import Config
    from .db_manager import init_db

    original = Config.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        Config.DB_PATH = os.path.join(tmp, "reference.db")
        try:
            init_db()
            conn = sqlite3.connect(Config.DB_PATH)
            try:
                return {t: _columns(conn, t) for t in _tables(conn)}
            finally:
                conn.close()
        finally:
            Config.DB_PATH = original


def _too_strict(have: tuple, want: tuple) -> bool:
    """Required (NOT NULL, no default) here, but optional or defaulted in the current schema."""
    old_required = have[3] and have[4] is None and not have[5]
    new_required = want[3] and want[4] is None and not want[5]
    return bool(old_required and not new_required)


def _backup(db_path: str) -> str:
    """Copy of the database before any rebuild; named *.db.bak so git ignores it."""
    stem = os.path.splitext(db_path)[0]
    path = f"{stem}-pre-repair-{time.strftime('%Y%m%d-%H%M%S')}.db.bak"
    shutil.copy2(db_path, path)
    return path


def _relax_table(conn: sqlite3.Connection, table: str, fixes: Dict[str, tuple]) -> None:
    """Rebuild `table` with the given columns made optional (with the current default)."""
    create_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    new_sql = create_sql
    for name, want in fixes.items():
        pattern = re.compile(rf'(\b{re.escape(name)}\b[^,\n]*?)\s+NOT\s+NULL', re.I)
        match = pattern.search(new_sql)
        if not match:
            raise RuntimeError(f"Could not relax {table}.{name}")
        head = match.group(1)
        if want[4] is not None and "DEFAULT" not in head.upper():
            head += f" DEFAULT {want[4]}"
        new_sql = new_sql[:match.start()] + head + new_sql[match.end():]
    temp = f"{table}__repair_new"
    new_sql = re.sub(rf'^\s*CREATE\s+TABLE\s+("?){re.escape(table)}\1', f'CREATE TABLE "{temp}"', new_sql, count=1, flags=re.I)
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
    columns = ", ".join(f'"{c}"' for c in _columns(conn, table))
    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(new_sql)
        conn.execute(f'INSERT INTO "{temp}" ({columns}) SELECT {columns} FROM "{table}"')
        after = conn.execute(f'SELECT COUNT(*) FROM "{temp}"').fetchone()[0]
        if after != before:
            raise RuntimeError(f"{table}: copied {after} of {before} rows")
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{table}"')
        for sql in extras:
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def repair(db_path: str) -> List[Tuple[str, str]]:
    """Add every column the code expects but `db_path` lacks. Returns what was added."""
    reference = reference_schema()
    added: List[Tuple[str, str]] = []
    conn = sqlite3.connect(db_path)
    try:
        existing_tables = set(_tables(conn))
        for table, wanted in reference.items():
            if table not in existing_tables:
                continue          # whole tables are created by init_db / migrations already
            have = _columns(conn, table)
            for name, info in wanted.items():
                if name in have or info[5]:          # never add a primary key
                    continue
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {_column_ddl(info)}')
                added.append((table, name))
        conn.commit()

        # Columns this database still marks required but the code treats as optional.
        strict: Dict[str, Dict[str, tuple]] = {}
        for table, wanted in reference.items():
            if table not in existing_tables:
                continue
            for name, info in _columns(conn, table).items():
                if name in wanted and _too_strict(info, wanted[name]):
                    strict.setdefault(table, {})[name] = wanted[name]
        if strict:
            conn.isolation_level = None          # manage the rebuild transaction ourselves
            backup = _backup(db_path)
            print(f"✓ Backed up database before repair: {os.path.basename(backup)}")
            for table, fixes in strict.items():
                _relax_table(conn, table, fixes)
                added += [(table, f"{name} (now optional)") for name in fixes]
    finally:
        conn.close()
    for table, name in added:
        logger.info("Schema repair: added %s.%s", table, name)
    if added:
        print(f"✓ Schema repaired: added {len(added)} missing column(s): "
              + ", ".join(f"{t}.{c}" for t, c in added))
    return added

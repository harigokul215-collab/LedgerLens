"""SQLite storage: one database for every currency, with migrations and legacy import."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant         TEXT    NOT NULL DEFAULT '',
    merchant_key     TEXT    NOT NULL DEFAULT '',
    purchase_date    TEXT    NOT NULL,              -- YYYY-MM-DD
    total            REAL    NOT NULL,
    currency         TEXT    NOT NULL,
    fx_rate          REAL    NOT NULL DEFAULT 1.0,  -- base units per 1 unit of currency
    total_base       REAL    NOT NULL,
    base_currency    TEXT    NOT NULL,
    fx_estimated     INTEGER NOT NULL DEFAULT 0,
    subtotal         REAL,
    tax              REAL,
    tip              REAL,
    payment_method   TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT 'Other',
    purpose          TEXT    NOT NULL DEFAULT '',
    notes            TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '',   -- comma separated
    is_business      INTEGER NOT NULL DEFAULT 0,
    reimbursable     INTEGER NOT NULL DEFAULT 0,
    reimburse_status TEXT    NOT NULL DEFAULT 'n/a',
    image_path       TEXT    NOT NULL DEFAULT '',
    image_hash       TEXT    NOT NULL DEFAULT '',
    raw_text         TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT 'manual', -- ai | ocr | manual | import | legacy | demo
    confidence       REAL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_receipts_date     ON receipts(purchase_date);
CREATE INDEX IF NOT EXISTS ix_receipts_merchant ON receipts(merchant_key);
CREATE INDEX IF NOT EXISTS ix_receipts_hash     ON receipts(image_hash);

CREATE TABLE IF NOT EXISTS line_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id  INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    description TEXT    NOT NULL DEFAULT '',
    quantity    REAL,
    unit_price  REAL,
    amount      REAL
);
CREATE INDEX IF NOT EXISTS ix_items_receipt ON line_items(receipt_id);

CREATE TABLE IF NOT EXISTS budgets (
    category      TEXT PRIMARY KEY,
    monthly_limit REAL NOT NULL               -- in the base currency
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    purpose      TEXT NOT NULL DEFAULT '',
    hits         INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date  TEXT NOT NULL,
    base  TEXT NOT NULL,
    quote TEXT NOT NULL,
    rate  REAL NOT NULL,                      -- quote units per 1 base unit
    PRIMARY KEY (date, base, quote)
);
"""

RECEIPT_FIELDS = {
    "merchant", "merchant_key", "purchase_date", "total", "currency", "fx_rate", "total_base",
    "base_currency", "fx_estimated", "subtotal", "tax", "tip", "payment_method", "category",
    "purpose", "notes", "tags", "is_business", "reimbursable", "reimburse_status",
    "image_path", "image_hash", "raw_text", "source", "confidence",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "base_currency": "USD",
    "default_currency": "USD",
    "categories": config.DEFAULT_CATEGORIES,
    "ai_enabled": True,
    "ai_effort": "medium",
    "live_fx": True,
}

_db_path: Path = config.DB_PATH


def set_db_path(path: Path | str) -> None:
    """Point the module at another database (used by tests)."""
    global _db_path
    _db_path = Path(path)


def db_path() -> Path:
    return _db_path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


_local = threading.local()


def _conn() -> sqlite3.Connection:
    """One long-lived connection per thread (Streamlit runs each session on its own thread).

    Opening and closing per call forces a WAL checkpoint every time, which is
    ~10 ms per operation on Windows.
    """
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _db_path:
        if conn is not None:
            conn.close()
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # durable with WAL, no fsync per commit
        _local.conn, _local.path = conn, _db_path
    return conn


@contextmanager
def connect():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_SUFFIXES = r"\b(inc|llc|ltd|pvt|private|limited|co|corp|corporation|company|store|stores|#\s*\d+|no\.?\s*\d+)\b"


def merchant_key(name: str) -> str:
    """Normalise a merchant name so 'STARBUCKS #1234' and 'Starbucks' match."""
    s = (name or "").lower()
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    return " ".join(s.split())


# --------------------------------------------------------------------------- init
def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value))
            )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('schema_version', ?)",
            (json.dumps(SCHEMA_VERSION),),
        )
    _import_legacy()


def _import_legacy() -> None:
    """One-time import of the old expenses_usd.db / expenses_inr.db files."""
    if get_setting("legacy_imported", False):
        return
    imported = 0
    for currency, path in config.LEGACY_DBS.items():
        if not path.exists():
            continue
        try:
            with sqlite3.connect(path) as old:
                old.row_factory = sqlite3.Row
                rows = old.execute("SELECT * FROM expenses").fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            keys = r.keys()
            date = r["purchase_date"] or datetime.today().strftime("%Y-%m-%d")
            category = r["category"] if "category" in keys and r["category"] else "Other"
            legacy_map = {
                "Office Supplies": "Office & Business",
                "Meals & Entertainment": "Dining",
                "Travel & Transport": "Transport",
                "Utilities & Bills": "Bills & Utilities",
                "Software & Subscriptions": "Subscriptions",
                "General": "Other",
            }
            insert_receipt(
                {
                    "merchant": r["merchant"] or "",
                    "purchase_date": date,
                    "total": float(r["price"] or 0),
                    "currency": r["currency"] or currency,
                    "fx_rate": 1.0,
                    "total_base": float(r["price"] or 0),
                    "base_currency": r["currency"] or currency,
                    "fx_estimated": 1,  # re-priced by fx.reprice_stale()
                    "purpose": r["purpose"] or "",
                    "category": legacy_map.get(category, category),
                    "source": "legacy",
                },
                [],
            )
            imported += 1
    set_setting("legacy_imported", True)
    if imported:
        set_setting("legacy_import_count", imported)


# ----------------------------------------------------------------------- settings
def get_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def categories() -> list[str]:
    cats = get_setting("categories", config.DEFAULT_CATEGORIES)
    return cats if "Other" in cats else [*cats, "Other"]


# ----------------------------------------------------------------------- receipts
def _clean(fields: dict) -> dict:
    out = {k: v for k, v in fields.items() if k in RECEIPT_FIELDS}
    if "merchant" in out:
        out["merchant_key"] = merchant_key(out["merchant"])
    for flag in ("is_business", "reimbursable", "fx_estimated"):
        if flag in out:
            out[flag] = int(bool(out[flag]))
    return out


def insert_receipt(fields: dict, items: Iterable[dict] = ()) -> int:
    data = _clean(fields)
    data["created_at"] = data["updated_at"] = now_iso()
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO receipts ({cols}) VALUES ({marks})", list(data.values()))
        rid = int(cur.lastrowid)
        _write_items(conn, rid, items)
    return rid


def _write_items(conn: sqlite3.Connection, rid: int, items: Iterable[dict]) -> None:
    for it in items:
        desc = str(it.get("description") or "").strip()
        if not desc and it.get("amount") in (None, ""):
            continue
        conn.execute(
            "INSERT INTO line_items(receipt_id, description, quantity, unit_price, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, desc, _num(it.get("quantity")), _num(it.get("unit_price")), _num(it.get("amount"))),
        )


def _num(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update_receipt(rid: int, fields: dict, items: Iterable[dict] | None = None) -> None:
    data = _clean(fields)
    if data:
        data["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in data)
        with connect() as conn:
            conn.execute(f"UPDATE receipts SET {sets} WHERE id = ?", [*data.values(), rid])
    if items is not None:
        with connect() as conn:
            conn.execute("DELETE FROM line_items WHERE receipt_id = ?", (rid,))
            _write_items(conn, rid, items)


def delete_receipts(ids: Iterable[int]) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with connect() as conn:
        paths = [
            r["image_path"]
            for r in conn.execute(f"SELECT image_path FROM receipts WHERE id IN ({marks})", ids)
            if r["image_path"]
        ]
        conn.execute(f"DELETE FROM receipts WHERE id IN ({marks})", ids)
        still_used = {
            r["image_path"]
            for r in conn.execute("SELECT DISTINCT image_path FROM receipts WHERE image_path != ''")
        }
    for p in set(paths) - still_used:
        f = config.DATA_DIR / p
        if f.exists():
            f.unlink()
    return len(ids)


def get_receipt(rid: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def line_items(rid: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT description, quantity, unit_price, amount FROM line_items "
            "WHERE receipt_id = ? ORDER BY id",
            (rid,),
        ).fetchall()
    return [dict(r) for r in rows]


def receipts_df() -> pd.DataFrame:
    with connect() as conn:
        df = pd.read_sql_query("SELECT * FROM receipts ORDER BY purchase_date DESC, id DESC", conn)
    df["date"] = pd.to_datetime(df["purchase_date"], errors="coerce")
    for flag in ("is_business", "reimbursable", "fx_estimated"):
        df[flag] = df[flag].astype(bool)
    return df


def all_line_items() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT li.*, r.merchant, r.purchase_date, r.currency FROM line_items li "
            "JOIN receipts r ON r.id = li.receipt_id",
            conn,
        )


def find_duplicates(image_hash: str, merchant: str, date: str, total: float) -> list[dict]:
    """Exact image matches, plus same merchant + date + amount (within one cent)."""
    with connect() as conn:
        rows = []
        if image_hash:
            rows += conn.execute(
                "SELECT id, merchant, purchase_date, total, currency, 'Same image' AS reason "
                "FROM receipts WHERE image_hash = ?",
                (image_hash,),
            ).fetchall()
        key = merchant_key(merchant)
        if key and date:
            rows += conn.execute(
                "SELECT id, merchant, purchase_date, total, currency, "
                "'Same merchant, date and amount' AS reason FROM receipts "
                "WHERE merchant_key = ? AND purchase_date = ? AND ABS(total - ?) < 0.01",
                (key, date, float(total or 0)),
            ).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(dict(r))
    return out


# ------------------------------------------------------------------------ budgets
def budgets() -> dict[str, float]:
    with connect() as conn:
        return {r["category"]: r["monthly_limit"] for r in conn.execute("SELECT * FROM budgets")}


def set_budgets(limits: dict[str, float]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM budgets")
        for cat, amt in limits.items():
            if amt and amt > 0:
                conn.execute("INSERT INTO budgets(category, monthly_limit) VALUES (?, ?)", (cat, amt))


# ----------------------------------------------------------------- merchant rules
def learn_merchant(merchant: str, category: str, purpose: str = "") -> None:
    key = merchant_key(merchant)
    if not key:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO merchant_rules(merchant_key, display_name, category, purpose, hits, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(merchant_key) DO UPDATE SET "
            "display_name = excluded.display_name, category = excluded.category, "
            "purpose = CASE WHEN excluded.purpose != '' THEN excluded.purpose ELSE merchant_rules.purpose END, "
            "hits = merchant_rules.hits + 1, updated_at = excluded.updated_at",
            (key, merchant.strip(), category, purpose.strip(), now_iso()),
        )


def merchant_rule(merchant: str) -> dict | None:
    key = merchant_key(merchant)
    if not key:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM merchant_rules WHERE merchant_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def merchant_rules_df() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT merchant_key, display_name, category, purpose, hits FROM merchant_rules "
            "ORDER BY hits DESC, display_name",
            conn,
        )


def replace_merchant_rules(df: pd.DataFrame) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM merchant_rules")
        for r in df.to_dict("records"):
            name = str(r.get("display_name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO merchant_rules VALUES (?, ?, ?, ?, ?, ?)",
                (merchant_key(name), name, r.get("category") or "Other",
                 r.get("purpose") or "", int(r.get("hits") or 1), now_iso()),
            )


# ---------------------------------------------------------------------- fx cache
def cached_rates(base: str, quote: str, start: str, end: str) -> dict[str, float]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT date, rate FROM fx_rates WHERE base = ? AND quote = ? AND date BETWEEN ? AND ?",
            (base, quote, start, end),
        ).fetchall()
    return {r["date"]: r["rate"] for r in rows}


def latest_cached_rate_on_or_before(base: str, quote: str, date: str) -> tuple[str, float] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT date, rate FROM fx_rates WHERE base = ? AND quote = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (base, quote, date),
        ).fetchone()
        if row is None:  # nothing earlier: take the closest later date instead
            row = conn.execute(
                "SELECT date, rate FROM fx_rates WHERE base = ? AND quote = ? "
                "ORDER BY date ASC LIMIT 1",
                (base, quote),
            ).fetchone()
    return (row["date"], row["rate"]) if row else None


def store_rates(base: str, quote: str, rates: dict[str, float]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fx_rates(date, base, quote, rate) VALUES (?, ?, ?, ?)",
            [(d, base, quote, r) for d, r in rates.items()],
        )


def run_readonly_query(sql: str, limit: int = 200) -> pd.DataFrame:
    """Execute one SELECT statement on a read-only connection (used by the AI assistant)."""
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"(?is)^\s*(select|with)\b", stripped):
        raise ValueError("Only SELECT queries are allowed.")
    uri = f"file:{_db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        def authorizer(action, *_):
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        conn.set_authorizer(authorizer)
        df = pd.read_sql_query(stripped, conn)
    finally:
        conn.close()
    return df.head(limit)

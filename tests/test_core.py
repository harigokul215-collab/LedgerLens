"""Core tests: run with `python -m pytest -q` from the project root. No network or API key needed."""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, db, demo, fx, insights, ocr, pipeline, reports  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "receipts")
    monkeypatch.setattr(config, "LEGACY_DBS", {})
    db.set_db_path(tmp_path / "test.db")
    db.init_db()
    db.set_setting("live_fx", False)  # deterministic: use fallback rates
    yield


# ------------------------------------------------------------------ OCR parsing
RECEIPT_US = """WHOLE FOODS MARKET
123 Main St, Austin TX
Tel 512-555-0100
11/03/2026 14:22
Organic Bananas      2.49
Greek Yogurt         6.99
SUBTOTAL            9.48
Sales Tax 8.25%      0.78
TOTAL              $10.26
VISA ****1234      10.26
"""

RECEIPT_IN = """Cafe Coffee Day
GSTIN 29ABCDE1234F1Z5
Date: 04/11/2026
Cappuccino   1   180.00
Sandwich     1   220.00
Sub Total        400.00
CGST 2.5%         10.00
SGST 2.5%         10.00
Grand Total   Rs 420.00
"""


def test_parse_us_receipt():
    out = ocr.parse_text(RECEIPT_US, today=date(2026, 11, 24))
    assert out["merchant"] == "Whole Foods Market"
    assert out["total"] == 10.26
    assert out["currency"] == "USD"
    assert out["purchase_date"] == "2026-11-03"
    assert out["tax"] == 0.78


def test_parse_indian_receipt_day_first_and_gst():
    out = ocr.parse_text(RECEIPT_IN, today=date(2026, 11, 24))
    assert out["total"] == 420.00          # grand total, not subtotal
    assert out["currency"] == "INR"
    assert out["purchase_date"] == "2026-11-04"  # 04/11 read day-first for INR
    assert out["tax"] == 20.00
    assert out["merchant"] == "Cafe Coffee Day"


def test_ocr_category_payment_and_items():
    out = ocr.parse_text(RECEIPT_IN + "\nPaid via UPI\n", today=date(2026, 11, 24))
    assert out["category"] == "Dining"
    assert out["payment_method"] == "UPI"
    assert [i["description"] for i in out["line_items"]] == ["Cappuccino", "Sandwich"]
    assert out["line_items"][0]["amount"] == 180.00
    us = ocr.parse_text(RECEIPT_US, today=date(2026, 11, 24))
    assert us["category"] == "Groceries" and us["payment_method"] == "Card"
    assert {"Organic Bananas", "Greek Yogurt"} <= {i["description"] for i in us["line_items"]}


def test_date_formats():
    t = date(2026, 11, 24)
    assert ocr.find_date("Nov 5, 2026", today=t) == "2026-11-05"
    assert ocr.find_date("05 Nov 2026", today=t) == "2026-11-05"
    assert ocr.find_date("2026-10-31", today=t) == "2026-10-31"
    assert ocr.find_date("12/31/2030", today=t) is None  # future dates rejected


def test_thousands_separator():
    assert ocr.amounts_in("TOTAL 1,234.50") == [1234.50]


# ------------------------------------------------------------------ storage
def test_merchant_key_normalises():
    assert db.merchant_key("STARBUCKS #1234") == db.merchant_key("Starbucks") == "starbucks"
    assert db.merchant_key("Acme Pvt. Ltd.") == "acme"


def test_save_converts_and_learns():
    rid = pipeline.save({"merchant": "Swiggy", "purchase_date": "2026-11-01", "total": 927.0,
                         "currency": "INR", "category": "Dining", "purpose": "Lunch"})
    r = db.get_receipt(rid)
    assert r["base_currency"] == "USD"
    assert r["total_base"] == pytest.approx(927 / config.FALLBACK_USD_RATES["INR"], rel=1e-3)
    assert r["fx_estimated"] == 1
    assert db.merchant_rule("SWIGGY")["category"] == "Dining"


def test_duplicates_detected():
    pipeline.save({"merchant": "Uber", "purchase_date": "2026-11-02", "total": 18.5,
                   "currency": "USD", "category": "Transport"})
    dups = db.find_duplicates("", "UBER", "2026-11-02", 18.5)
    assert len(dups) == 1


def test_update_reconverts_on_currency_change():
    rid = pipeline.save({"merchant": "X", "purchase_date": "2026-11-02", "total": 100,
                         "currency": "USD", "category": "Other"})
    pipeline.update(rid, {"currency": "EUR"})
    r = db.get_receipt(rid)
    assert r["total_base"] == pytest.approx(100 / config.FALLBACK_USD_RATES["EUR"], rel=1e-3)


def test_reprice_after_base_change():
    pipeline.save({"merchant": "X", "purchase_date": "2026-11-02", "total": 10,
                   "currency": "USD", "category": "Other"})
    db.set_setting("base_currency", "INR")
    updated, _ = fx.reprice()
    assert updated == 1
    assert db.receipts_df()["total_base"].iloc[0] == pytest.approx(10 * config.FALLBACK_USD_RATES["INR"])


def test_readonly_query_blocks_writes():
    with pytest.raises(Exception):
        db.run_readonly_query("DELETE FROM receipts")
    with pytest.raises(Exception):
        db.run_readonly_query("SELECT 1; DROP TABLE receipts")
    assert db.run_readonly_query("SELECT COUNT(*) AS n FROM receipts")["n"].iloc[0] == 0


def test_delete_and_line_items():
    rid = pipeline.save({"merchant": "Shop", "purchase_date": "2026-11-02", "total": 5, "currency": "USD",
                         "category": "Other"}, [{"description": "Pen", "quantity": 1, "unit_price": 5, "amount": 5}])
    assert db.line_items(rid)[0]["description"] == "Pen"
    db.delete_receipts([rid])
    assert db.get_receipt(rid) is None
    assert db.line_items(rid) == []


# ------------------------------------------------------------------ insights
def test_demo_and_insights():
    n = demo.generate()
    assert n > 100
    df = db.receipts_df()
    rec = insights.recurring(df)
    found = set(rec["merchant"])
    assert {"Netflix", "Spotify", "Adobe Creative Cloud"} <= found
    netflix = rec[rec["merchant"] == "Netflix"].iloc[0]
    assert netflix["cadence"] == "Monthly"
    summary = insights.month_summary(df)
    assert summary["projected"] >= summary["mtd"]
    anomalies = insights.anomalies(df)
    assert "Best Buy" in set(anomalies["merchant"])
    cards = insights.feed(df, db.budgets(), "USD")
    titles = " ".join(c["title"] for c in cards)
    assert "Netflix raised its price" in titles
    assert "awaiting reimbursement" in titles
    assert demo.clear() == n


def test_monthly_pivot_and_colors():
    demo.generate(months=3)
    df = db.receipts_df()
    pivot = insights.monthly_by_category(df, 6)
    assert len(pivot) == 6
    colors = insights.category_color_map(db.categories())
    folded = insights.fold_categories(pivot, colors)
    assert set(folded.columns) <= set(colors)
    assert len(set(colors.values())) == len(colors)  # no colour reused


def test_recurring_ignores_irregular():
    base = date(2026, 1, 1)
    for gap in (3, 40, 9, 70):
        base += timedelta(days=gap)
        pipeline.save({"merchant": "Random Shop", "purchase_date": base.isoformat(), "total": 20,
                       "currency": "USD", "category": "Shopping"})
    assert insights.recurring(db.receipts_df()).empty


# ------------------------------------------------------------------ reports
def test_exports():
    demo.generate(months=2)
    df = db.receipts_df()
    assert reports.to_csv(df).startswith(b"\xef\xbb\xbf")
    xlsx = reports.to_excel(df, "USD")
    assert zipfile.is_zipfile(io.BytesIO(xlsx))
    pdf = reports.expense_report_pdf(df.head(20), "USD", "Test report", "Tester")
    assert pdf[:5] == b"%PDF-"


def test_backup_restore_roundtrip():
    pipeline.save({"merchant": "Keep me", "purchase_date": "2026-11-02", "total": 1, "currency": "USD",
                   "category": "Other"})
    backup = reports.backup_zip()
    db.delete_receipts(db.receipts_df()["id"])
    assert db.receipts_df().empty
    assert reports.restore_zip(backup) == 1
    assert db.receipts_df()["merchant"].iloc[0] == "Keep me"

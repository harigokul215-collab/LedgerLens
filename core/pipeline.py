"""Scan -> review -> save. Turns an uploaded file into a draft, and a draft into a stored receipt."""
from __future__ import annotations

from datetime import date as Date

from . import ai, db, fx, imaging, ocr


def process(name: str, raw: bytes) -> dict:
    """Build an editable draft from an uploaded file. Never raises for bad extractions."""
    rf = imaging.load(name, raw)
    cats = db.categories()
    default_cur = db.get_setting("default_currency", "USD")
    warnings: list[str] = []
    result: dict = {}
    engine = "manual"

    if db.get_setting("ai_enabled", True) and ai.status()[0]:
        try:
            result = ai.extract(rf, cats, default_cur, db.get_setting("ai_effort", "medium"))
            engine = "ai"
            if not result.get("is_receipt", True):
                warnings.append("Claude doesn't think this is a receipt.")
            if result.get("notes"):
                warnings.append(result["notes"])
        except ai.AIUnavailable as exc:
            warnings.append(f"AI extraction unavailable: {exc} Fell back to OCR.")

    if engine != "ai":
        if ocr.tesseract_available():
            result = ocr.extract(rf.image)
            engine = "ocr"
        else:
            warnings.append("Tesseract isn't installed, so fields are blank. Fill them in by hand.")

    return build_draft(result, engine, warnings, rf)


def quick(text: str) -> dict:
    """Draft from a one-line description, via Claude. Raises ai.AIUnavailable."""
    result = ai.quick_entry(text, db.categories(), db.get_setting("default_currency", "USD"))
    result["raw_text"] = text
    return build_draft(result, "manual", [], None)


def build_draft(result: dict, engine: str, warnings: list[str], rf: imaging.ReceiptFile | None) -> dict:
    cats = db.categories()
    default_cur = db.get_setting("default_currency", "USD")
    draft = {
        "merchant": result.get("merchant") or "",
        "purchase_date": result.get("purchase_date"),
        "total": result.get("total"),
        "currency": result.get("currency") or default_cur,
        "subtotal": result.get("subtotal"),
        "tax": result.get("tax"),
        "tip": result.get("tip"),
        "payment_method": result.get("payment_method") or "",
        "category": result.get("category") or "Other",
        "purpose": result.get("purpose") or "",
        "notes": "",
        "tags": "",
        "is_business": False,
        "reimbursable": False,
        "line_items": result.get("line_items") or [],
        "raw_text": result.get("raw_text") or "",
        "confidence": result.get("confidence"),
        "source": engine,
        "image_hash": rf.sha256 if rf else "",
    }

    rule = db.merchant_rule(draft["merchant"])
    if rule:
        draft["merchant"] = rule["display_name"]
        draft["category"] = rule["category"] if rule["category"] in cats else draft["category"]
        draft["purpose"] = draft["purpose"] or rule["purpose"]
        draft["learned"] = True

    if not draft["purchase_date"]:
        draft["purchase_date"] = Date.today().isoformat()
        warnings.append("No date found; set to today.")
    if not draft["total"]:
        warnings.append("No total found. Please enter it.")
        draft["total"] = 0.0

    draft["duplicates"] = db.find_duplicates(draft["image_hash"], draft["merchant"],
                                             draft["purchase_date"], draft["total"])
    draft["warnings"] = warnings
    draft["file"] = rf
    return draft


def save(fields: dict, items: list[dict] | None = None, rf: imaging.ReceiptFile | None = None) -> int:
    """Convert to the base currency, store the image, insert, and learn the merchant."""
    base = db.get_setting("base_currency", "USD")
    total = float(fields["total"])
    converted, rate, est = fx.convert(total, fields["currency"], base, fields["purchase_date"])
    record = {
        **fields,
        "total": total,
        "total_base": converted,
        "fx_rate": rate,
        "fx_estimated": est,
        "base_currency": base,
    }
    if rf is not None:
        record["image_path"] = imaging.store(rf)
        record["image_hash"] = rf.sha256
    rid = db.insert_receipt(record, items or [])
    if fields.get("merchant") and fields.get("category") not in (None, "", "Other"):
        db.learn_merchant(fields["merchant"], fields["category"], fields.get("purpose", ""))
    return rid


def update(rid: int, fields: dict, items: list[dict] | None = None) -> None:
    """Edit a receipt, re-converting if the amount, currency or date changed."""
    current = db.get_receipt(rid)
    if current is None:
        return
    merged = {**current, **fields}
    if any(k in fields and fields[k] != current[k] for k in ("total", "currency", "purchase_date")):
        base = db.get_setting("base_currency", "USD")
        converted, rate, est = fx.convert(float(merged["total"]), merged["currency"], base, merged["purchase_date"])
        fields = {**fields, "total_base": converted, "fx_rate": rate, "fx_estimated": est, "base_currency": base}
    db.update_receipt(rid, fields, items)
    if "category" in fields and merged.get("merchant") and merged.get("category") != "Other":
        db.learn_merchant(merged["merchant"], merged["category"], merged.get("purpose", ""))

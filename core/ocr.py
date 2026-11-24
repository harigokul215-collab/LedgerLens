"""Offline extraction: OpenCV clean-up + Tesseract + receipt-aware text parsing.

Used when Claude is unavailable. The parser is deliberately separate from the
OCR call so it can be unit-tested on plain text.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import date as Date, datetime

import cv2
import numpy as np
from PIL import Image

_tesseract_ready: bool | None = None


def tesseract_available() -> bool:
    global _tesseract_ready
    if _tesseract_ready is None:
        import pytesseract

        if not shutil.which("tesseract"):
            for candidate in (
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                "/opt/homebrew/bin/tesseract",
                "/usr/local/bin/tesseract",
            ):
                if os.path.exists(candidate):
                    pytesseract.pytesseract.tesseract_cmd = candidate
                    break
        try:
            pytesseract.get_tesseract_version()
            _tesseract_ready = True
        except Exception:
            _tesseract_ready = False
    return _tesseract_ready


def preprocess(img: Image.Image) -> np.ndarray:
    """Greyscale, upscale small photos, denoise, deskew and binarise."""
    arr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    h, w = arr.shape
    if max(h, w) < 1500:
        scale = 1500 / max(h, w)
        arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    arr = cv2.fastNlMeansDenoising(arr, h=10)
    arr = _deskew(arr)
    return cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)


def _deskew(gray: np.ndarray) -> np.ndarray:
    inv = cv2.bitwise_not(gray)
    _, bw = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(bw > 0))
    if len(coords) < 500:
        return gray
    angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
    angle = angle - 90 if angle > 45 else angle
    if abs(angle) < 0.5 or abs(angle) > 15:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def ocr_text(img: Image.Image) -> str:
    import pytesseract

    if not tesseract_available():
        return ""
    processed = preprocess(img)
    best = ""
    for psm in (4, 6):  # column-aware first, then uniform block
        try:
            text = pytesseract.image_to_string(processed, config=f"--oem 3 --psm {psm}")
        except Exception:
            continue
        if len(re.findall(r"\d", text)) > len(re.findall(r"\d", best)):
            best = text
    return best


# --------------------------------------------------------------------- parsing
AMOUNT = r"(?<![\d.,])(\d{1,3}(?:[,\s]\d{3})+|\d+)(?:[.,](\d{2}))(?![\d])"
TOTAL_KEYS = [
    (r"grand\s*total", 10), (r"amount\s*(?:due|payable|paid)", 9), (r"balance\s*due", 9),
    (r"net\s*(?:amount|payable|total)", 8), (r"total\s*(?:amount|due|inr|usd|rs)?", 7),
    (r"(?:card|visa|mastercard|amex|upi)\s*(?:payment|amt|amount)?", 3),
]
NOT_TOTAL = r"sub\s*-?\s*total|total\s*(?:tax|gst|vat|savings|discount|items?|qty|quantity)|you\s*saved|tip\s*guide"
TAX_KEYS = r"\b(?:tax|gst|cgst|sgst|igst|vat|hst|pst|service\s*tax|sales\s*tax)\b"

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

CURRENCY_HINTS = [
    (r"₹|\brs\.?\s*\d|\binr\b|\bgst(?:in)?\b|\bcgst\b|\bsgst\b|\bupi\b", "INR"),
    (r"€|\beur\b", "EUR"), (r"£|\bgbp\b", "GBP"), (r"¥|\bjpy\b", "JPY"),
    (r"\bc\$|\bcad\b|\bhst\b", "CAD"), (r"\ba\$|\baud\b", "AUD"), (r"\bs\$|\bsgd\b", "SGD"),
    (r"\$|\busd\b", "USD"),
]


def _to_float(whole: str, cents: str) -> float:
    return float(re.sub(r"[,\s]", "", whole) + "." + cents)


def amounts_in(line: str) -> list[float]:
    return [_to_float(w, c) for w, c in re.findall(AMOUNT, line)]


def find_total(lines: list[str]) -> tuple[float | None, float]:
    """Return (total, confidence 0..1)."""
    candidates: list[tuple[int, int, float]] = []  # (score, line index, amount)
    for i, line in enumerate(lines):
        low = line.lower()
        if re.search(NOT_TOTAL, low):
            continue
        for pattern, score in TOTAL_KEYS:
            if re.search(pattern, low):
                vals = amounts_in(line)
                if not vals and i + 1 < len(lines):  # amount printed on the next line
                    vals = amounts_in(lines[i + 1])
                if vals:
                    candidates.append((score, i, vals[-1]))
                break
    if candidates:
        # Highest keyword score wins; among equals prefer the lowest line on the receipt.
        score, _, value = max(candidates, key=lambda c: (c[0], c[1]))
        return value, min(0.5 + score / 20, 0.95)
    every = [v for line in lines for v in amounts_in(line) if v < 1_000_000]
    if every:
        return max(every), 0.3
    return None, 0.0


def find_tax(lines: list[str]) -> float | None:
    total = 0.0
    found = False
    for line in lines:
        low = line.lower()
        if re.search(TAX_KEYS, low) and not re.search(r"incl|inclusive|before\s*tax|gstin|tax\s*invoice|tax\s*id", low):
            vals = amounts_in(line)
            if vals:
                total += vals[-1]
                found = True
    return round(total, 2) if found else None


def find_currency(text: str) -> str | None:
    low = text.lower()
    for pattern, code in CURRENCY_HINTS:
        if re.search(pattern, low):
            return code
    return None


def find_date(text: str, prefer_day_first: bool = False, today: Date | None = None) -> str | None:
    today = today or Date.today()
    found: list[Date] = []
    low = text.lower()

    for y, m, d in re.findall(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", low):
        found += _try(int(y), int(m), int(d))
    for a, b, y in re.findall(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", low):
        y = int(y) + (2000 if len(y) == 2 else 0)
        a, b = int(a), int(b)
        orders = [(b, a), (a, b)] if prefer_day_first or a > 12 else [(a, b), (b, a)]
        for m, d in orders:
            got = _try(y, m, d)
            if got:
                found += got
                break
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    for d, m, y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/]*{mon}[\s\-/,]*(\d{{2,4}})\b", low):
        y = int(y) + (2000 if len(y) == 2 else 0)
        found += _try(y, MONTHS[m[:3]], int(d))
    for m, d, y in re.findall(rf"\b{mon}\s*(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})\b", low):
        found += _try(int(y), MONTHS[m[:3]], int(d))

    plausible = [d for d in found if Date(2000, 1, 1) <= d <= today]
    return plausible[0].isoformat() if plausible else None


def _try(y: int, m: int, d: int) -> list[Date]:
    try:
        return [Date(y, m, d)]
    except ValueError:
        return []


SKIP_MERCHANT = re.compile(
    r"receipt|invoice|tax|gstin|welcome|thank|tel|phone|www\.|http|date|time|order|bill\s*no|cashier|table|\d{3}[-\s]\d{3}",
    re.I,
)


def find_merchant(lines: list[str]) -> str:
    for line in lines[:8]:
        clean = re.sub(r"[^A-Za-z0-9&'.\- ]", "", line).strip()
        letters = sum(ch.isalpha() for ch in clean)
        if letters >= 3 and not SKIP_MERCHANT.search(clean) and letters / max(len(clean), 1) > 0.5:
            return clean.title() if clean.isupper() else clean
    return ""


CATEGORY_HINTS = [
    ("Dining", r"cafe|coffee|restaurant|bistro|kitchen|pizza|burger|starbucks|swiggy|zomato|bar\b|grill|bakery|diner|table\s*\d"),
    ("Groceries", r"grocery|supermarket|mart\b|fresh|bigbasket|whole\s*foods|trader\s*joe|kroger|safeway|dmart|produce"),
    ("Transport", r"uber|ola\b|lyft|taxi|cab\b|fuel|petrol|diesel|gas\s*station|shell|parking|metro|toll"),
    ("Travel", r"hotel|airline|airways|flight|boarding|indigo|resort|booking|inn\b|check-?in"),
    ("Health", r"pharma|pharmacy|chemist|clinic|hospital|medical|apollo|cvs|walgreens|dental"),
    ("Bills & Utilities", r"electricity|water\s*bill|broadband|internet|mobile\s*bill|recharge|utility|power\b"),
    ("Entertainment", r"cinema|movie|pvr|inox|theatre|theater|concert|ticket"),
    ("Office & Business", r"office|staples|stationery|printing|cowork|wework"),
    ("Shopping", r"amazon|flipkart|mall|apparel|fashion|electronics|best\s*buy|target|walmart|ikea"),
]

PAYMENT_HINTS = [("UPI", r"\bupi\b|gpay|phonepe|paytm"), ("Card", r"visa|master\s*card|amex|debit|credit|card\b|\*{3,}\d{4}"),
                 ("Cash", r"\bcash\b"), ("Wallet", r"apple\s*pay|google\s*pay|wallet")]

ITEM_STOP = r"sub\s*-?\s*total|total|tax|gst|vat|tip|change|balance|cash|card|upi|paid|discount|round"


def guess_category(text: str, categories: list[str] | None = None) -> str | None:
    low = text.lower()
    for cat, pattern in CATEGORY_HINTS:
        if re.search(pattern, low) and (categories is None or cat in categories):
            return cat
    return None


def find_payment(text: str) -> str:
    low = text.lower()
    for method, pattern in PAYMENT_HINTS:
        if re.search(pattern, low):
            return method
    return ""


def find_items(lines: list[str]) -> list[dict]:
    """Lines shaped like 'Item name [qty] amount' above the subtotal/total block."""
    items = []
    for line in lines:
        if re.search(ITEM_STOP, line, re.I):
            if items:
                break  # reached the totals block
            continue
        m = re.match(rf"^(?P<desc>[A-Za-z][A-Za-z0-9&'()./\- ]{{2,}}?)\s+(?:(?P<qty>\d{{1,3}})\s+)?(?P<amt>{AMOUNT})\s*$", line)
        if m:
            amt = amounts_in(m.group("amt"))
            if amt:
                qty = float(m.group("qty")) if m.group("qty") else 1.0
                items.append({"description": m.group("desc").strip(), "quantity": qty,
                              "unit_price": round(amt[0] / qty, 2) if qty else None, "amount": amt[0]})
    return items


def parse_text(text: str, today: Date | None = None) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    currency = find_currency(text)
    total, total_conf = find_total(lines)
    date = find_date(text, prefer_day_first=currency in ("INR", "EUR", "GBP", "AUD", "SGD"), today=today)
    merchant = find_merchant(lines)
    signals = [total_conf, 0.8 if date else 0.2, 0.7 if merchant else 0.2, 0.7 if currency else 0.4]
    return {
        "merchant": merchant,
        "purchase_date": date,
        "total": total,
        "tax": find_tax(lines),
        "currency": currency,
        "category": guess_category(text),
        "payment_method": find_payment(text),
        "line_items": find_items(lines),
        "confidence": round(sum(signals) / len(signals), 2),
    }


def extract(img: Image.Image) -> dict:
    text = ocr_text(img)
    out = parse_text(text)
    out["raw_text"] = text
    return out

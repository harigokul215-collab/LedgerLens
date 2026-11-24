"""Static configuration shared by every module."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LEDGERLENS_DATA") or ROOT / "data")
IMAGE_DIR = DATA_DIR / "receipts"
DB_PATH = DATA_DIR / "receipts.db"

# Databases written by the original single-file version of the app.
LEGACY_DBS = {"USD": ROOT / "expenses_usd.db", "INR": ROOT / "expenses_inr.db"}

DEFAULT_CATEGORIES = [
    "Dining",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Utilities",
    "Subscriptions",
    "Travel",
    "Health",
    "Entertainment",
    "Office & Business",
    "Other",
]

# Every currency the ECB reference-rate feed (frankfurter.dev) can convert.
CURRENCIES = {
    "USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "C$",
    "AUD": "A$", "SGD": "S$", "CHF": "CHF ", "CNY": "CN¥", "HKD": "HK$",
    "NZD": "NZ$", "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "ZAR": "R ",
    "MXN": "MX$", "BRL": "R$", "KRW": "₩", "THB": "฿", "MYR": "RM ",
    "IDR": "Rp ", "PHP": "₱", "PLN": "zł ", "TRY": "₺", "ILS": "₪",
    "CZK": "Kč ", "HUF": "Ft ", "RON": "lei ", "BGN": "лв ", "ISK": "kr ",
}

# Used only when the rate service is unreachable and nothing is cached:
# approximate units of each currency per 1 USD. Conversions made with these
# are flagged as estimated and re-priced automatically once online.
FALLBACK_USD_RATES = {
    "USD": 1.0, "INR": 92.7, "EUR": 0.92, "GBP": 0.78, "JPY": 152.0, "CAD": 1.39,
    "AUD": 1.53, "SGD": 1.33, "CHF": 0.88, "CNY": 7.2, "HKD": 7.8, "NZD": 1.68,
    "SEK": 10.6, "NOK": 10.9, "DKK": 6.9, "ZAR": 18.0, "MXN": 19.5, "BRL": 5.6,
    "KRW": 1390.0, "THB": 34.5, "MYR": 4.4, "IDR": 16000.0, "PHP": 58.0,
    "PLN": 3.95, "TRY": 41.0, "ILS": 3.7, "CZK": 23.0, "HUF": 365.0,
    "RON": 4.6, "BGN": 1.8, "ISK": 138.0,
}

PAYMENT_METHODS = ["Card", "Cash", "UPI", "Bank transfer", "Wallet", "Other", ""]
REIMBURSE_STATES = ["n/a", "pending", "submitted", "reimbursed"]

AI_MODEL = "claude-opus-5-5"

# Chart colours: the validated categorical order (light mode) from the
# data-viz reference palette. Colour follows the category, never its rank;
# categories past slot 8 fold into "Other" in categorical charts.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER_GRAY = "#9a9994"
SEQ_BLUE = "#2a78d6"
STATUS = {"good": "#1baf7a", "warning": "#eda100", "critical": "#e34948"}


def symbol(currency: str) -> str:
    return CURRENCIES.get(currency, f"{currency} ")


def money(amount: float | None, currency: str, decimals: int | None = None) -> str:
    if amount is None:
        return "—"
    if decimals is None:
        decimals = 0 if currency in ("JPY", "KRW", "IDR", "HUF", "ISK") else 2
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol(currency)}{abs(amount):,.{decimals}f}"

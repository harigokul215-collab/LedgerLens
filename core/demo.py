"""Realistic sample data so every feature can be explored before scanning real receipts."""
from __future__ import annotations

import random
from datetime import date as Date, timedelta

from . import db, fx, pipeline

SUBSCRIPTIONS = [  # merchant, category, currency, amount, day of month, purpose
    ("Netflix", "Subscriptions", "USD", 15.49, 3, "Streaming"),
    ("Spotify", "Subscriptions", "INR", 119.0, 9, "Music"),
    ("Adobe Creative Cloud", "Subscriptions", "USD", 22.99, 14, "Design tools"),
    ("iCloud+", "Subscriptions", "USD", 2.99, 21, "Cloud storage"),
    ("Cult.fit", "Health", "INR", 1999.0, 1, "Gym membership"),
    ("Airtel Broadband", "Bills & Utilities", "INR", 999.0, 6, "Home internet"),
]

FREQUENT = [  # merchant, category, currency, (lo, hi), visits per month, purpose
    ("Starbucks", "Dining", "USD", (4.5, 9.5), 6, "Coffee"),
    ("Swiggy", "Dining", "INR", (240, 820), 5, "Food delivery"),
    ("Whole Foods Market", "Groceries", "USD", (38, 140), 3, "Weekly groceries"),
    ("BigBasket", "Groceries", "INR", (650, 2400), 3, "Groceries"),
    ("Uber", "Transport", "USD", (11, 34), 3, "Ride"),
    ("Ola", "Transport", "INR", (140, 460), 3, "Cab"),
    ("Amazon", "Shopping", "USD", (14, 95), 2, "Household items"),
    ("Apollo Pharmacy", "Health", "INR", (180, 900), 1, "Medicines"),
    ("PVR Cinemas", "Entertainment", "INR", (420, 1100), 1, "Movie night"),
]

ITEMS = {
    "Starbucks": [("Caffè Latte", 5.25), ("Blueberry Muffin", 3.45), ("Cold Brew", 4.95)],
    "Whole Foods Market": [("Organic Bananas", 2.49), ("Greek Yogurt", 6.99), ("Sourdough", 5.99),
                           ("Avocados (4)", 6.00), ("Chicken Breast", 12.80)],
    "BigBasket": [("Toor Dal 1kg", 165.0), ("Basmati Rice 5kg", 699.0), ("Amul Butter", 285.0),
                  ("Fresh Vegetables", 340.0)],
}


def _items_for(merchant: str, total: float, rng: random.Random) -> list[dict]:
    pool = ITEMS.get(merchant)
    if not pool:
        return []
    chosen = rng.sample(pool, k=min(len(pool), rng.randint(2, 4)))
    raw = sum(p for _, p in chosen)
    return [{"description": d, "quantity": 1, "unit_price": round(p * total / raw, 2),
             "amount": round(p * total / raw, 2)} for d, p in chosen]


def generate(months: int = 7, seed: int = 7) -> int:
    rng = random.Random(seed)
    today = Date.today()
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    base = db.get_setting("base_currency", "USD")
    fx.prefetch(base, {"USD": (start.isoformat(), today.isoformat()),
                       "INR": (start.isoformat(), today.isoformat())})
    rows: list[tuple[dict, list]] = []

    def add(d: Date, merchant, cat, cur, amount, purpose, **extra):
        if d > today:
            return
        fields = {"merchant": merchant, "purchase_date": d.isoformat(), "total": round(amount, 2),
                  "currency": cur, "category": cat, "purpose": purpose, "source": "demo",
                  "payment_method": "UPI" if cur == "INR" and rng.random() < .6 else "Card", **extra}
        rows.append((fields, _items_for(merchant, amount, rng)))

    m = start
    while m <= today:
        last_day = ((m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        for merchant, cat, cur, amt, day, purpose in SUBSCRIPTIONS:
            price = amt
            if merchant == "Netflix" and m >= (today.replace(day=1) - timedelta(days=1)).replace(day=1):
                price = 17.99  # price hike the insights feed should notice
            add(m.replace(day=min(day, last_day)), merchant, cat, cur, price, purpose)
        for merchant, cat, cur, (lo, hi), visits, purpose in FREQUENT:
            for _ in range(max(0, int(rng.gauss(visits, 1)))):
                add(m.replace(day=rng.randint(1, last_day)), merchant, cat, cur, rng.uniform(lo, hi), purpose)
        add(m.replace(day=min(18, last_day)), "Tata Power", "Bills & Utilities", "INR",
            rng.uniform(1700, 2700), "Electricity bill")
        if rng.random() < 0.6:
            add(m.replace(day=rng.randint(1, last_day)), "Staples", "Office & Business", "USD",
                rng.uniform(18, 75), "Office supplies", is_business=True, reimbursable=True,
                reimburse_status="reimbursed" if m < today.replace(day=1) else "pending")
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)

    # A business trip last month, partly still pending reimbursement.
    trip = (today.replace(day=1) - timedelta(days=1)).replace(day=12)
    for offset, merchant, cat, cur, amt, purpose in [
        (0, "IndiGo", "Travel", "INR", 6840, "Flight BLR to DEL, client visit"),
        (0, "Taj Palace", "Travel", "INR", 14250, "Hotel, 2 nights"),
        (1, "Bukhara", "Dining", "INR", 4380, "Client dinner"),
        (2, "Uber", "Transport", "INR", 640, "Airport transfer"),
    ]:
        add(trip + timedelta(days=offset), merchant, cat, cur, amt, purpose,
            is_business=True, reimbursable=True, reimburse_status="pending", tags="client-visit")
    # An unusually large purchase this month.
    add(today - timedelta(days=min(2, today.day - 1)), "Best Buy", "Shopping", "USD", 649.99,
        "Noise-cancelling headphones", notes="Extended warranty included")

    for fields, items in rows:
        pipeline.save(fields, items)
    if not db.budgets():
        conv = 1.0 if base == "USD" else fx.rate("USD", base, today.isoformat())[0]
        db.set_budgets({k: round(v * conv, -1 if conv > 10 else 0) for k, v in {
            "Dining": 260, "Groceries": 420, "Transport": 160, "Shopping": 250,
            "Subscriptions": 90, "Entertainment": 40}.items()})
    return len(rows)


def clear() -> int:
    df = db.receipts_df()
    ids = df.loc[df["source"] == "demo", "id"].tolist()
    return db.delete_receipts(ids)

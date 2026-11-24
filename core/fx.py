"""Historical exchange rates (ECB reference rates via frankfurter.dev) with a local cache.

Every receipt is converted to the base currency at the rate on its purchase
date. When the service can't be reached, a built-in approximate table is used
and the receipt is flagged ``fx_estimated``; ``reprice()`` fixes those later.
"""
from __future__ import annotations

import time
from datetime import date as Date, datetime, timedelta

import requests

from . import config, db

API = "https://api.frankfurter.dev/v1"
_offline_until = 0.0  # back off after a network failure so the UI stays fast


def _live_enabled() -> bool:
    return bool(db.get_setting("live_fx", True)) and time.time() >= _offline_until


def _fetch_series(base: str, quote: str, start: str, end: str) -> dict[str, float]:
    """quote units per 1 base, keyed by date. Empty dict on any failure."""
    global _offline_until
    if not _live_enabled() or quote not in config.CURRENCIES or base not in config.CURRENCIES:
        return {}
    try:
        resp = requests.get(
            f"{API}/{start}..{end}", params={"base": base, "symbols": quote}, timeout=6
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        _offline_until = time.time() + 300
        return {}
    rates = {d: v[quote] for d, v in payload.get("rates", {}).items() if quote in v}
    if rates:
        db.store_rates(base, quote, rates)
    return rates


def _fallback(currency: str, base: str) -> float:
    usd = config.FALLBACK_USD_RATES
    if currency in usd and base in usd:
        return usd[base] / usd[currency]
    return 1.0


def rate(currency: str, base: str, on: str | Date) -> tuple[float, bool]:
    """Return (base units per 1 unit of ``currency`` on that date, is_estimated)."""
    if currency == base:
        return 1.0, False
    day = on if isinstance(on, str) else on.isoformat()
    today = Date.today().isoformat()
    day = min(day, today)

    hit = db.latest_cached_rate_on_or_before(base, currency, day)
    if hit and (datetime.fromisoformat(day) - datetime.fromisoformat(hit[0])).days <= 5:
        return 1.0 / hit[1], False

    start = (datetime.fromisoformat(day) - timedelta(days=10)).date().isoformat()
    series = _fetch_series(base, currency, start, day)
    if series:
        best = max(d for d in series if d <= day) if any(d <= day for d in series) else min(series)
        return 1.0 / series[best], False

    if hit:  # stale but real
        return 1.0 / hit[1], True
    return _fallback(currency, base), True


def convert(amount: float, currency: str, base: str, on: str | Date) -> tuple[float, float, bool]:
    """Return (converted amount, rate used, estimated?)."""
    r, est = rate(currency, base, on)
    return round(amount * r, 4), r, est


def prefetch(base: str, needs: dict[str, tuple[str, str]]) -> None:
    """Warm the cache: needs maps currency -> (earliest date, latest date)."""
    today = Date.today().isoformat()
    for cur, (lo, hi) in needs.items():
        if cur == base:
            continue
        lo = (datetime.fromisoformat(lo) - timedelta(days=10)).date().isoformat()
        hi = min(hi, today)
        have = db.cached_rates(base, cur, lo, hi)
        span = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).days
        if span > 0 and len(have) >= span * 5 / 7 * 0.9:
            continue  # roughly every business day is already cached
        _fetch_series(base, cur, lo, hi)


def reprice(force: bool = False) -> tuple[int, int]:
    """Re-convert receipts whose base currency changed or whose rate was estimated.

    Returns (rows updated, rows still estimated).
    """
    base = db.get_setting("base_currency", "USD")
    df = db.receipts_df()
    if df.empty:
        return 0, 0
    stale = df if force else df[(df["base_currency"] != base) | df["fx_estimated"]]
    if stale.empty:
        return 0, 0
    needs = {
        cur: (g["purchase_date"].min(), g["purchase_date"].max())
        for cur, g in stale.groupby("currency")
    }
    prefetch(base, needs)
    updated = still_est = 0
    for r in stale.itertuples():
        amount, fx_rate, est = convert(float(r.total), r.currency, base, r.purchase_date)
        db.update_receipt(
            int(r.id),
            {"total_base": amount, "fx_rate": fx_rate, "base_currency": base, "fx_estimated": est},
        )
        updated += 1
        still_est += int(est)
    return updated, still_est

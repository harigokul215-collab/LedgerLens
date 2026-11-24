"""Claude-powered receipt understanding.

Credentials resolve the standard way: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN,
an `ant auth login` profile, or a key pasted into the Settings page (kept in
memory for the session only, never written to disk).
"""
from __future__ import annotations

import json
import os
from datetime import date as Date
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from . import config, imaging

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_session_key: str | None = None
_disabled_reason: str | None = None


class AIUnavailable(RuntimeError):
    pass


def set_session_key(key: str | None) -> None:
    global _session_key, _disabled_reason
    new = (key or "").strip() or None
    if new != _session_key:
        _session_key = new
        _disabled_reason = None  # a new key deserves a fresh try


def has_explicit_credentials() -> bool:
    return bool(_session_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def status() -> tuple[bool, str]:
    """(usable?, human-readable reason)."""
    if _disabled_reason:
        return False, _disabled_reason
    if _session_key:
        return True, "Using the API key entered in Settings"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True, "Using ANTHROPIC_API_KEY from the environment"
    if os.environ.get("ANTHROPIC_PROFILE") or (Path.home() / ".config" / "anthropic").exists():
        return True, "Using your `ant auth login` profile"
    return False, "No Claude API key yet: using offline OCR. Add a key in Settings for AI extraction."


def available() -> bool:
    """True when Claude features should be shown: AI is switched on and credentials exist."""
    from . import db

    return bool(db.get_setting("ai_enabled", True)) and status()[0]


def client() -> anthropic.Anthropic:
    try:
        return anthropic.Anthropic(api_key=_session_key) if _session_key else anthropic.Anthropic()
    except Exception as exc:  # no credentials resolvable at all
        raise AIUnavailable(f"No Claude credentials found ({exc}). Add a key in Settings.") from exc


def _disable(reason: str) -> None:
    global _disabled_reason
    _disabled_reason = reason


def call(**kwargs):
    """messages.create / parse with refusal fallback and friendly errors."""
    method = kwargs.pop("_method", "create")
    try:
        fn = getattr(client().beta.messages, method)
        resp = fn(model=config.AI_MODEL, betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    except anthropic.AuthenticationError as exc:
        _disable("Claude rejected the credentials. Check the API key in Settings.")
        raise AIUnavailable(_disabled_reason) from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIUnavailable("This API key can't use the model.") from exc
    except anthropic.RateLimitError as exc:
        raise AIUnavailable("Rate limited by the Claude API. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise AIUnavailable(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIUnavailable("Couldn't reach the Claude API (offline?).") from exc
    except TypeError as exc:
        if "authentication" not in str(exc).lower():
            raise
        _disable("No Claude credentials found. Add an API key in Settings.")
        raise AIUnavailable(_disabled_reason) from exc
    if resp.stop_reason == "refusal":
        raise AIUnavailable("Claude declined this request.")
    return resp


# ------------------------------------------------------------------ extraction
class LineItem(BaseModel):
    description: str
    quantity: float | None
    unit_price: float | None
    amount: float | None


class ReceiptData(BaseModel):
    is_receipt: bool = Field(description="False if the image is not a receipt, invoice or bill")
    merchant: str = Field(description="Business name as a person would say it, e.g. 'Starbucks'")
    purchase_date: str | None = Field(description="YYYY-MM-DD, or null if not printed")
    currency: str = Field(description="ISO 4217 code, e.g. USD, INR, EUR")
    total: float | None = Field(description="Final amount paid, including tax and tip")
    subtotal: float | None
    tax: float | None = Field(description="Sum of all tax lines (GST/CGST/SGST/VAT/sales tax)")
    tip: float | None
    payment_method: str = Field(description="One of: Card, Cash, UPI, Bank transfer, Wallet, Other, or ''")
    category: str
    purpose: str = Field(description="Short guess at what this purchase was for, at most 6 words")
    line_items: list[LineItem]
    confidence: float = Field(description="0 to 1: how sure you are about merchant, date and total")
    notes: str = Field(description="Anything the person should double-check, or ''")


SYSTEM = """You read photographed or scanned receipts and extract structured data for an expense tracker.

Rules:
- total is the amount actually paid: after discounts, including tax, service charge and tip. If a handwritten tip and total exist, use the handwritten total.
- Dates: output YYYY-MM-DD. For ambiguous numeric dates, use the receipt's country (India, UK, EU, Australia write day first; the US writes month first). Never invent a date: use null if none is printed.
- currency: infer from symbols, country, address, phone format or tax names (GST/CGST/SGST imply INR in India; HST implies CAD). Use the default currency given only if nothing indicates otherwise.
- category: pick exactly one from the list provided.
- line_items: every purchased item with its line amount; skip tax, subtotal and payment lines. Empty list if items are unreadable.
- Lower confidence for blurry, cropped or handwritten receipts and explain in notes."""


def extract(rf: imaging.ReceiptFile, categories: list[str], default_currency: str, effort: str = "medium") -> dict:
    prompt = (
        f"Today is {Date.today().isoformat()}. Default currency: {default_currency}.\n"
        f"Categories: {', '.join(categories)}.\n"
        "Extract this receipt."
    )
    resp = call(
        _method="parse",
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": effort},
        output_format=ReceiptData,
        messages=[{"role": "user", "content": [imaging.ai_payload(rf), {"type": "text", "text": prompt}]}],
    )
    data: ReceiptData | None = resp.parsed_output
    if data is None:
        raise AIUnavailable("Claude's answer couldn't be read as a receipt.")
    out = data.model_dump()
    out["category"] = _snap(out["category"], categories)
    out["currency"] = (out["currency"] or default_currency).upper()
    if out["currency"] not in config.CURRENCIES:
        out["notes"] = (out["notes"] + f" Currency {out['currency']} isn't supported for conversion.").strip()
        out["currency"] = default_currency
    if out["payment_method"] not in config.PAYMENT_METHODS:
        out["payment_method"] = "Other" if out["payment_method"] else ""
    out["raw_text"] = json.dumps(data.model_dump(), indent=1, ensure_ascii=False)
    return out


def _snap(value: str, options: list[str]) -> str:
    if value in options:
        return value
    low = (value or "").lower()
    for opt in options:
        if opt.lower() in low or low in opt.lower():
            return opt
    return "Other" if "Other" in options else options[-1]


# ----------------------------------------------------------------- quick entry
class QuickEntry(BaseModel):
    merchant: str
    purchase_date: str = Field(description="YYYY-MM-DD; resolve words like 'yesterday' from today's date")
    total: float
    currency: str = Field(description="ISO 4217 code")
    category: str
    purpose: str
    payment_method: str = Field(description="One of: Card, Cash, UPI, Bank transfer, Wallet, Other, or ''")


def quick_entry(text: str, categories: list[str], default_currency: str) -> dict:
    """'Lunch at Subway 12.50 yesterday' -> receipt fields."""
    resp = call(
        _method="parse",
        max_tokens=4000,
        output_config={"effort": "low"},
        output_format=QuickEntry,
        system=("Turn a one-line expense description into structured fields. "
                f"Today is {Date.today().isoformat()} ({Date.today():%A}). Default currency: {default_currency}. "
                f"Pick category exactly from: {', '.join(categories)}. Keep purpose to a few words."),
        messages=[{"role": "user", "content": text}],
    )
    data: QuickEntry | None = resp.parsed_output
    if data is None:
        raise AIUnavailable("Couldn't understand that description.")
    out = data.model_dump()
    out["category"] = _snap(out["category"], categories)
    out["currency"] = out["currency"].upper() if out["currency"].upper() in config.CURRENCIES else default_currency
    if out["payment_method"] not in config.PAYMENT_METHODS:
        out["payment_method"] = ""
    return out


# ---------------------------------------------------------------- monthly review
def monthly_review(stats: dict, base_currency: str) -> str:
    resp = call(
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=(
            "You are a sharp, friendly personal-finance analyst. Write a short monthly spending review "
            "in Markdown from the JSON facts given: 1) a two-sentence headline, 2) three to five specific "
            "observations with numbers, 3) two or three concrete, realistic suggestions. "
            f"Amounts are in {base_currency}. Don't invent facts beyond the data. Under 250 words."
        ),
        messages=[{"role": "user", "content": json.dumps(stats, default=str)}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()

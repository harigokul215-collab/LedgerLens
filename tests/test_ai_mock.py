"""Exercise the Claude code paths against a mocked HTTP transport (no key, no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anthropic
import httpx2
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ai, assistant, config, db, imaging, pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "receipts")
    monkeypatch.setattr(config, "LEGACY_DBS", {})
    db.set_db_path(tmp_path / "ai.db")
    db.init_db()
    db.set_setting("live_fx", False)


def message(content: list, stop: str = "end_turn") -> dict:
    return {"id": "msg_x", "type": "message", "role": "assistant", "model": config.AI_MODEL,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def install(monkeypatch, responses: list[dict], seen: list[dict]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx2.Response(200, json=responses[len(seen) - 1])
    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(ai, "client", lambda: client)


def test_extract_receipt(monkeypatch, tmp_path):
    payload = {"is_receipt": True, "merchant": "Blue Tokai", "purchase_date": "2026-11-21", "currency": "INR",
               "total": 1039.5, "subtotal": 990.0, "tax": 49.5, "tip": None, "payment_method": "UPI",
               "category": "dining", "purpose": "Coffee meeting",
               "line_items": [{"description": "Cappuccino", "quantity": 2, "unit_price": 220, "amount": 440}],
               "confidence": 0.94, "notes": ""}
    seen: list[dict] = []
    install(monkeypatch, [message([{"type": "text", "text": json.dumps(payload)}])], seen)

    img = tmp_path / "r.png"
    Image.new("RGB", (400, 700), "white").save(img)
    rf = imaging.load("r.png", img.read_bytes())
    out = ai.extract(rf, db.categories(), "USD", "medium")

    assert out["merchant"] == "Blue Tokai" and out["total"] == 1039.5
    assert out["category"] == "Dining"          # snapped to the user's list
    body, headers = seen[0]["body"], seen[0]["headers"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["type"] == "image"


def test_pipeline_falls_back_to_ocr_on_refusal(monkeypatch, tmp_path):
    seen: list[dict] = []
    install(monkeypatch, [message([], stop="refusal")], seen)
    monkeypatch.setattr(ai, "status", lambda: (True, "test"))
    img = tmp_path / "r.png"
    Image.new("RGB", (400, 700), "white").save(img)
    draft = pipeline.process("r.png", img.read_bytes())
    assert draft["source"] in ("ocr", "manual")
    assert any("declined" in w for w in draft["warnings"])


def test_assistant_tool_loop(monkeypatch):
    pipeline.save({"merchant": "Uber", "purchase_date": "2026-11-02", "total": 20, "currency": "USD",
                   "category": "Transport"})
    responses = [
        message([{"type": "tool_use", "id": "tu_1", "name": "run_sql",
                  "input": {"sql": "SELECT SUM(total_base) AS spent FROM receipts WHERE category='Transport'"}},
                 {"type": "tool_use", "id": "tu_2", "name": "show_chart",
                  "input": {"title": "By category", "sql": "SELECT category, SUM(total_base) AS s FROM receipts GROUP BY 1",
                            "chart_type": "bar", "x": "category", "y": "s"}}], stop="tool_use"),
        message([{"type": "text", "text": "You spent **$20.00** on transport."}]),
    ]
    seen: list[dict] = []
    install(monkeypatch, responses, seen)
    history, turn = assistant.ask([], "How much on transport?", "USD", db.categories())

    assert turn.text.startswith("You spent")
    assert len(turn.charts) == 1 and turn.charts[0]["df"]["s"].iloc[0] == 20
    second = seen[1]["body"]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0]["type"] == "tool_use"
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]   # both results in ONE user message
    assert "20.0" in results[0]["content"]
    assert all(t.get("strict") for t in seen[0]["body"]["tools"])
    assert len(history) == 4


def test_assistant_reports_sql_errors(monkeypatch):
    responses = [
        message([{"type": "tool_use", "id": "tu_1", "name": "run_sql", "input": {"sql": "DELETE FROM receipts"}}],
                stop="tool_use"),
        message([{"type": "text", "text": "I can only read data."}]),
    ]
    seen: list[dict] = []
    install(monkeypatch, responses, seen)
    _, turn = assistant.ask([], "delete everything", "USD", db.categories())
    err = seen[1]["body"]["messages"][2]["content"][0]
    assert err["is_error"] is True
    assert turn.text == "I can only read data."

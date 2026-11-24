"""Smoke test: every page renders without exceptions, empty and with demo data."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["views/dashboard.py", "views/scan.py", "views/ledger.py", "views/budgets.py", "views/insights.py",
         "views/reimburse.py", "views/settings.py"]
AI_PAGE = "views/assistant.py"


@pytest.fixture(scope="module")
def data_dir():
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        os.environ.pop(var, None)
    tmp = tempfile.mkdtemp()
    os.environ["LEDGERLENS_DATA"] = tmp
    sys.path.insert(0, str(ROOT))
    import importlib
    from core import config, db
    importlib.reload(config)
    db.set_db_path(config.DB_PATH)
    yield tmp


def run_page(page: str):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    if page != PAGES[0]:
        at.switch_page(page)
        at.run()
    return at


@pytest.mark.parametrize("page", PAGES)
def test_pages_empty(data_dir, page):
    at = run_page(page)
    assert not at.exception, [e.value for e in at.exception]


def test_load_demo(data_dir):
    from core import db, demo
    db.init_db()
    db.set_setting("live_fx", False)
    assert demo.generate() > 100


@pytest.mark.parametrize("page", PAGES)
def test_pages_with_data(data_dir, page):
    at = run_page(page)
    assert not at.exception, [e.value for e in at.exception]


def test_ai_features_hidden_without_key(data_dir):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    at.switch_page(AI_PAGE)  # not registered in navigation: falls back to the dashboard
    at.run()
    assert at.title[0].value == "Dashboard"
    at = run_page("views/insights.py")
    assert all("AI" not in t.label for t in at.tabs)
    at = run_page("views/scan.py")
    assert [t.label for t in at.tabs][-1] == "✍️ Manual entry"


def test_ai_features_shown_with_key(data_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    at = run_page(AI_PAGE)
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "Ask your receipts"
    at = run_page("views/insights.py")
    assert any("AI monthly review" in t.label for t in at.tabs)
    at = run_page("views/scan.py")
    assert [t.label for t in at.tabs][-1] == "⚡ Quick add"

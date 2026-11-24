# 🧾 LedgerLens

Snap a receipt and the app reads it: merchant, date, total, tax, line items, currency, payment method and a suggested category. Every receipt is converted to your reporting currency at the historical rate for its purchase date. The app then turns the pile into budgets, subscription tracking, unusual-charge alerts and reimbursement reports.

Everything runs locally: one SQLite file plus a folder of receipt images. **No API key is needed.** Receipts are read with offline OCR (OpenCV + Tesseract). Adding a Claude API key is optional and unlocks the AI features marked ✨ below; they stay hidden until a key is configured.

## What it does

| | |
|---|---|
| **📷 Scan** | Batch-upload photos or PDFs, or use the camera. An offline OpenCV + Tesseract pipeline cleans the image (deskew, denoise) and reads the merchant, date, the real total (grand total, not subtotal), GST/VAT tax lines and line items, with day-first dates for INR/EUR/GBP and category and payment guesses. ✨ With a key, Claude (`claude-opus-5-5`) reads the receipt instead and handles blurry or handwritten ones better. |
| **✍️ Manual entry** | Record cash or receipt-less expenses. ✨ With a key this becomes *Quick add*: type "Lunch at Truffles ₹1,450 yesterday, UPI" and get a filled-in receipt to confirm. |
| **🧠 Merchant memory** | Each save teaches the app which category and purpose a merchant belongs to. The next "STARBUCKS #1234" is filed automatically. Rules are editable in Settings. |
| **👯 Duplicate guard** | Warns when an image was already scanned, or when a receipt matches an existing merchant, date and amount. |
| **💱 Any currency** | 31 currencies. Each receipt keeps its original amount and is converted at the ECB reference rate for its date (cached locally). If you're offline, estimated rates are used and fixed automatically later. Change the reporting currency and the whole ledger is re-priced. |
| **📊 Dashboard** | This month vs last (running totals), month-end projection that accounts for upcoming subscriptions, 12-month trend by category, top merchants, weekday pattern and a prioritised "needs your attention" feed. |
| **🎯 Budgets** | Monthly limits per category, pace markers ("on pace for $861"), status badges, a six-month history and one-click suggestions from your averages. |
| **💡 Insights** | Subscription detection (weekly to yearly, price-hike alerts, annual cost), unusual-charge detection (a robust z-score per category), category shifts vs your typical month, and a tax and business summary. ✨ With a key: an AI-written monthly review. |
| **💼 Reimbursements** | Pending → submitted → reimbursed workflow. Generates a PDF claim with a summary, an itemised table and every receipt image, or a ZIP with PDF, CSV and images. |
| **✨ Ask your receipts** | *(Needs a key.)* Chat with your data. Claude writes read-only SQL against your database (authoriser-enforced, single SELECT only), answers in plain language and draws charts inline. Every query it ran is shown. |
| **🧾 Receipts ledger** | Full-text search (including line items), filters, inline editing, bulk re-categorise, tag or delete, and export to CSV, Excel (with monthly and category sheets) or PDF. |
| **🗄️ Data** | One-click backup/restore (database + images), CSV import from bank statements with column mapping, demo data, and automatic one-time import of the old `expenses_usd.db` / `expenses_inr.db`. |

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

**Install Tesseract** (the OCR engine used to read receipts):
- Windows: [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki)
- macOS: `brew install tesseract`
- Linux: `sudo apt install tesseract-ocr`

**Optional: AI features.** Set `ANTHROPIC_API_KEY` or paste a key in **Settings → AI (optional)**. A pasted key is kept in memory for that session only and never written to disk. Never commit a key to the repository. Once a key is present, the ✨ features appear automatically. Requests use server-side refusal fallback, so a declined request is retried on a fallback model.

**Try it instantly.** On first launch click **Load demo data**: seven months of realistic USD and INR receipts with subscriptions, a business trip and an unusual purchase. Remove them any time in Settings.

## Layout

```
app.py              navigation and sidebar
core/
  db.py             SQLite schema, CRUD, merchant rules, FX cache, read-only query runner
  pipeline.py       file → draft (AI or OCR) → review → save (with FX conversion)
  ai.py             Claude extraction, quick entry, monthly review
  assistant.py      tool-use loop for "Ask your receipts"
  ocr.py            OpenCV preprocessing + Tesseract + receipt text parser
  fx.py             historical exchange rates with cache and offline fallback
  insights.py       projections, budgets, subscriptions, anomalies, smart feed
  reports.py        CSV / Excel / PDF / ZIP exports, backup and restore
  imaging.py        photo/PDF loading, EXIF rotation, hashing, storage
  demo.py           sample data generator
views/              one Streamlit page per file
tests/              core logic, mocked Claude API, and every page rendered headlessly
```

Data lives in `data/` (override with the `LEDGERLENS_DATA` environment variable). Run the tests with `python -m pytest -q`.

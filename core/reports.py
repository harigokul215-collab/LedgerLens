"""Exports: Excel workbook, PDF expense report (with receipt images), ZIP bundles, backup/restore."""
from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config, db, imaging

EXPORT_COLUMNS = [
    "id", "purchase_date", "merchant", "category", "purpose", "total", "currency", "total_base",
    "base_currency", "fx_rate", "subtotal", "tax", "tip", "payment_method", "tags", "is_business",
    "reimbursable", "reimburse_status", "notes", "source",
]


def tidy(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in EXPORT_COLUMNS if c in df.columns]].copy()


def to_csv(df: pd.DataFrame) -> bytes:
    return tidy(df).to_csv(index=False).encode("utf-8-sig")  # BOM so Excel shows ₹ correctly


def to_excel(df: pd.DataFrame, base: str) -> bytes:
    buf = io.BytesIO()
    data = tidy(df)
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        data.to_excel(xw, sheet_name="Receipts", index=False)
        if not df.empty:
            monthly = df.assign(month=df["date"].dt.strftime("%Y-%m")).pivot_table(
                index="month", columns="category", values="total_base", aggfunc="sum", fill_value=0)
            monthly["Total"] = monthly.sum(axis=1)
            monthly.round(2).to_excel(xw, sheet_name=f"Monthly ({base})")
            cat = df.groupby("category").agg(total=("total_base", "sum"), receipts=("id", "count"))
            cat.sort_values("total", ascending=False).round(2).to_excel(xw, sheet_name=f"Categories ({base})")
            ids = set(df["id"])
            items = db.all_line_items()
            items = items[items["receipt_id"].isin(ids)]
            if not items.empty:
                items.drop(columns=["id"]).to_excel(xw, sheet_name="Line items", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "B2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 9), 48)
    return buf.getvalue()


# --------------------------------------------------------------------- PDF report
_FONT_CANDIDATES = [  # (regular, bold) with good coverage of ₹ € £ ¥
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]


def _pdf_with_font():
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists():
            try:
                pdf.add_font("Body", "", regular)
                pdf.add_font("Body", "B", bold if Path(bold).exists() else regular)
                return pdf, "Body", True
            except Exception:
                continue
    return pdf, "Helvetica", False


def expense_report_pdf(df: pd.DataFrame, base: str, title: str, submitter: str = "",
                       include_images: bool = True) -> bytes:
    pdf, font, unicode_ok = _pdf_with_font()

    def t(s) -> str:
        s = "" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s)
        return s if unicode_ok else s.encode("latin-1", "replace").decode("latin-1")

    def amt(v, cur) -> str:
        return f"{cur} {v:,.2f}"

    df = df.sort_values("date")
    pdf.add_page()
    pdf.set_font(font, "B", 20)
    pdf.cell(0, 12, t(title or "Expense report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.set_text_color(90, 90, 90)
    period = (f"{df['date'].min():%d %b %Y} – {df['date'].max():%d %b %Y}" if not df.empty else "—")
    meta = [f"Period: {period}", f"Receipts: {len(df)}", f"Generated: {datetime.now():%d %b %Y %H:%M}"]
    if submitter:
        meta.insert(0, f"Submitted by: {submitter}")
    for line in meta:
        pdf.cell(0, 5.5, t(line), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    pdf.set_font(font, "B", 14)
    pdf.cell(0, 9, t(f"Total: {amt(df['total_base'].sum(), base)}"), new_x="LMARGIN", new_y="NEXT")
    natives = df.groupby("currency")["total"].sum()
    if len(natives) > 1 or (len(natives) == 1 and natives.index[0] != base):
        pdf.set_font(font, "", 9)
        pdf.cell(0, 5, t("Original currencies: " + ", ".join(amt(v, c) for c, v in natives.items())),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # By category
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 7, "By category", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 9.5)
    with pdf.table(col_widths=(70, 30, 40), text_align=("LEFT", "RIGHT", "RIGHT"), width=140,
                   align="LEFT", first_row_as_headings=True) as table:
        table.row(["Category", "Receipts", f"Amount ({base})"])
        for cat, g in df.groupby("category"):
            table.row([t(cat), str(len(g)), f"{g['total_base'].sum():,.2f}"])
    pdf.ln(5)

    # Itemised list
    pdf.set_font(font, "B", 11)
    pdf.cell(0, 7, "Receipts", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 8.5)
    with pdf.table(col_widths=(8, 20, 40, 28, 44, 26, 24),
                   text_align=("RIGHT", "LEFT", "LEFT", "LEFT", "LEFT", "RIGHT", "RIGHT"),
                   first_row_as_headings=True) as table:
        table.row(["#", "Date", "Merchant", "Category", "Purpose", "Amount", base])
        for i, r in enumerate(df.itertuples(), start=1):
            table.row([str(i), f"{r.date:%d %b %Y}", t(r.merchant)[:40], t(r.category), t(r.purpose)[:60],
                       amt(r.total, r.currency), f"{r.total_base:,.2f}"])

    if include_images:
        for i, r in enumerate(df.itertuples(), start=1):
            path = imaging.stored_path(r.image_path)
            if not path:
                continue
            pdf.add_page()
            pdf.set_font(font, "B", 11)
            pdf.cell(0, 7, t(f"#{i}  {r.merchant}  ·  {r.date:%d %b %Y}  ·  {amt(r.total, r.currency)}"),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
            from PIL import Image

            with Image.open(path) as im:
                w, h = im.size
            max_w, max_h = pdf.epw, pdf.eph - 20
            scale = min(max_w / w, max_h / h)
            pdf.image(str(path), w=w * scale, h=h * scale)

    return bytes(pdf.output())


def bundle_zip(df: pd.DataFrame, base: str, title: str, submitter: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("expenses.csv", to_csv(df))
        z.writestr("report.pdf", expense_report_pdf(df, base, title, submitter, include_images=True))
        for i, r in enumerate(df.sort_values("date").itertuples(), start=1):
            path = imaging.stored_path(r.image_path)
            if path:
                safe = "".join(ch for ch in str(r.merchant) if ch.isalnum() or ch in " -_")[:30].strip() or "receipt"
                z.write(path, f"receipts/{i:03d}_{r.purchase_date}_{safe}.jpg")
    return buf.getvalue()


# ------------------------------------------------------------------ backup / restore
def backup_zip() -> bytes:
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "receipts.db"
        src = sqlite3.connect(db.db_path())
        dst = sqlite3.connect(snap)
        src.backup(dst)  # consistent snapshot even while the app is running
        dst.close()
        src.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "receipts.db")
            if config.IMAGE_DIR.exists():
                for f in config.IMAGE_DIR.iterdir():
                    if f.is_file():
                        z.write(f, f"receipts/{f.name}")
    return buf.getvalue()


def restore_zip(raw: bytes) -> int:
    """Replace the current data with a backup. Returns the number of receipts restored."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        if "receipts.db" not in names:
            raise ValueError("This isn't a LedgerLens backup (receipts.db missing).")
        with tempfile.TemporaryDirectory() as tmp:
            z.extract("receipts.db", tmp)
            src = sqlite3.connect(Path(tmp) / "receipts.db")
            try:
                count = src.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                # The backup API copies page-by-page into the live database, so open
                # connections on other threads see the restored data safely.
                with db.connect() as live:
                    src.backup(live)
            except sqlite3.Error as exc:
                raise ValueError(f"Backup database is unreadable: {exc}") from exc
            finally:
                src.close()
        config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        for n in names:
            if n.startswith("receipts/") and not n.endswith("/"):
                name = Path(n).name
                if name and ".." not in n:
                    (config.IMAGE_DIR / name).write_bytes(z.read(n))
    db.init_db()
    return int(count)


def import_csv(raw: bytes, mapping: dict[str, str], default_currency: str) -> pd.DataFrame:
    """Normalise a bank/card export into receipt fields using a user-chosen column mapping."""
    df = pd.read_csv(io.BytesIO(raw))
    out = pd.DataFrame()
    out["merchant"] = df[mapping["merchant"]].astype(str).str.strip()
    out["purchase_date"] = pd.to_datetime(df[mapping["date"]], errors="coerce", dayfirst=mapping.get("dayfirst") == "yes").dt.strftime("%Y-%m-%d")
    amounts = pd.to_numeric(df[mapping["amount"]].astype(str).str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")
    out["total"] = amounts.abs()
    out["currency"] = df[mapping["currency"]].astype(str).str.upper().str.strip() if mapping.get("currency") else default_currency
    out["category"] = df[mapping["category"]].astype(str) if mapping.get("category") else ""
    return out.dropna(subset=["purchase_date", "total"])

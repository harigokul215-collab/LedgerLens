"""Load receipt files (photos or PDFs), normalise them, hash them and store them."""
from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from . import config

MAX_STORE_EDGE = 2200   # stored copy for viewing / reports
MAX_AI_EDGE = 1568      # vision models gain nothing from larger images


@dataclass
class ReceiptFile:
    name: str
    raw: bytes
    sha256: str
    is_pdf: bool
    image: Image.Image          # first page / photo, upright RGB

    @property
    def stored_name(self) -> str:
        return f"receipts/{self.sha256[:24]}.jpg"


def load(name: str, raw: bytes) -> ReceiptFile:
    is_pdf = raw[:5] == b"%PDF-" or name.lower().endswith(".pdf")
    img = _render_pdf(raw) if is_pdf else _open_image(raw)
    return ReceiptFile(name=name, raw=raw, sha256=hashlib.sha256(raw).hexdigest(), is_pdf=is_pdf, image=img)


def _open_image(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)  # phone photos arrive rotated
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    return img.convert("RGB")


def _render_pdf(raw: bytes, max_pages: int = 3) -> Image.Image:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=raw, filetype="pdf")
    pages = []
    for page in list(doc)[:max_pages]:
        pix = page.get_pixmap(dpi=200)
        pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    doc.close()
    if not pages:
        raise ValueError("PDF has no pages")
    if len(pages) == 1:
        return pages[0]
    width = max(p.width for p in pages)
    sheet = Image.new("RGB", (width, sum(p.height for p in pages)), "white")
    y = 0
    for p in pages:
        sheet.paste(p, (0, y))
        y += p.height
    return sheet


def _shrink(img: Image.Image, edge: int) -> Image.Image:
    if max(img.size) <= edge:
        return img
    img = img.copy()
    img.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return img


def jpeg_bytes(img: Image.Image, edge: int, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    _shrink(img, edge).save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def ai_payload(rf: ReceiptFile) -> dict:
    """Content block for the Claude API: the PDF itself, or a right-sized JPEG."""
    if rf.is_pdf and len(rf.raw) < 20_000_000:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(rf.raw).decode()},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(jpeg_bytes(rf.image, MAX_AI_EDGE)).decode()},
    }


def store(rf: ReceiptFile) -> str:
    """Save a viewable copy under data/receipts and return its path relative to data/."""
    path = config.DATA_DIR / rf.stored_name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(jpeg_bytes(rf.image, MAX_STORE_EDGE, quality=85))
    return rf.stored_name


def stored_path(rel: str):
    if not rel:
        return None
    p = config.DATA_DIR / rel
    return p if p.exists() else None

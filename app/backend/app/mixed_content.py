import json
import os
import re
from pathlib import Path
from typing import Any

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .db import utc_now


def extract_page_text_ocr(image_path: str) -> str:
    """Extract ordinary text from a scanned page image.

    This is used for paragraph/context text. Formula recognition is handled by
    formula_layout/formula_structure separately.
    """
    lang = os.environ.get("PAGE_TEXT_OCR_LANG", "eng+kor")
    img = Image.open(image_path)
    variants = _prepare_text_ocr_variants(img)
    configs = [
        "--oem 3 --psm 6 -c preserve_interword_spaces=1",
        "--oem 3 --psm 4 -c preserve_interword_spaces=1",
        "--oem 3 --psm 3 -c preserve_interword_spaces=1",
    ]
    best = ""
    best_score = -1.0
    for variant in variants:
        for config in configs:
            try:
                text = pytesseract.image_to_string(variant, lang=lang, config=config)
            except Exception:
                continue
            text = clean_page_text(text)
            score = _score_text_ocr(text)
            if score > best_score:
                best = text
                best_score = score
    return best


def clean_page_text(text: str) -> str:
    s = (text or "").replace("\r", "\n")
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # Join hyphenated line breaks in English paragraphs.
    s = re.sub(r"([A-Za-z])-\n([A-Za-z])", r"\1\2", s)
    # Join line breaks inside normal paragraphs, but keep blank-line boundaries.
    paragraphs = []
    for para in re.split(r"\n\s*\n", s):
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue
        paragraphs.append(" ".join(lines))
    return "\n\n".join(paragraphs).strip()


def split_text_to_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if len(para) >= 3:
            blocks.append(para)
    return blocks


def insert_content_block(
    conn,
    document_id: str,
    page_no: int,
    block_seq: int,
    block_type: str,
    role: str | None = None,
    text_content: str | None = None,
    latex: str | None = None,
    formula_id: int | None = None,
    bbox: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO page_content_blocks(
            document_id,page_no,block_seq,block_type,role,text_content,latex,
            formula_id,bbox_json,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            document_id,
            page_no,
            block_seq,
            block_type,
            role,
            text_content,
            latex,
            formula_id,
            json.dumps(bbox, ensure_ascii=False) if bbox is not None else None,
            json.dumps(metadata or {}, ensure_ascii=False),
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def reset_page_content_blocks(conn, document_id: str, page_no: int) -> None:
    conn.execute(
        "DELETE FROM page_content_blocks WHERE document_id=? AND page_no=?",
        (document_id, page_no),
    )


def build_mixed_content_blocks(
    conn,
    document_id: str,
    page_no: int,
    page_text: str,
    formulas: list[dict[str, Any]],
    source: str,
) -> int:
    """Store text and formula blocks in a simple reading order.

    For PDF text extraction, exact geometric ordering is difficult without a full
    layout model. This function stores paragraph text first, then formulas in
    formula_seq order. When formula candidates have bbox, metadata preserves it
    so a later layout model can improve ordering.
    """
    reset_page_content_blocks(conn, document_id, page_no)
    seq = 1
    for text_block in split_text_to_blocks(page_text):
        insert_content_block(
            conn,
            document_id,
            page_no,
            seq,
            "text",
            role="paragraph",
            text_content=text_block,
            metadata={"source": source},
        )
        seq += 1

    for formula in sorted(formulas, key=lambda x: int(x.get("formula_seq") or 0)):
        insert_content_block(
            conn,
            document_id,
            page_no,
            seq,
            "formula",
            role="equation",
            text_content=formula.get("raw_text"),
            latex=formula.get("latex"),
            formula_id=formula.get("formula_id"),
            bbox=formula.get("bbox"),
            metadata={
                "source": formula.get("source_type"),
                "confidence": formula.get("confidence"),
                "status": formula.get("status"),
            },
        )
        seq += 1
    return seq - 1


def _prepare_text_ocr_variants(img: Image.Image) -> list[Image.Image]:
    gray = ImageOps.grayscale(ImageOps.exif_transpose(img))
    variants: list[Image.Image] = []
    for scale in [2, 3]:
        enlarged = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
        enhanced = ImageEnhance.Contrast(enlarged).enhance(1.6)
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.4)
        variants.append(enhanced)
        threshold = _auto_threshold(enhanced)
        binary = enhanced.point(lambda p, t=threshold: 255 if p > t else 0)
        variants.append(binary.filter(ImageFilter.MedianFilter(size=3)))
    return variants


def _auto_threshold(img: Image.Image) -> int:
    hist = img.histogram()
    total = sum(hist)
    if total == 0:
        return 180
    weighted_sum = sum(i * count for i, count in enumerate(hist))
    mean = weighted_sum / total
    return max(130, min(220, int(mean * 0.90)))


def _score_text_ocr(text: str) -> float:
    if not text:
        return -1.0
    score = min(len(text), 2000) / 50.0
    score += 2.0 * len(re.findall(r"\b(?:the|and|of|insurance|premium|life|table|column|sum)\b", text, flags=re.IGNORECASE))
    score += len(re.findall(r"[A-Za-z]{4,}", text)) / 4.0
    score -= 3.0 * len(re.findall(r"[□■◆●�]", text))
    return score

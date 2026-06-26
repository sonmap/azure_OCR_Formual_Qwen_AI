import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .formula_ocr import recognize_formula_image
from .formula_structure import (
    STRUCTURED_FORMULA_PROMPT,
    formula_diagnostics,
    repair_formula_latex,
    structure_quality_score,
)


def recognize_formula_regions(image_path: str, engine: str | None = None) -> list[dict[str, Any]]:
    """Recognize a formula image using whole-image and detected-region OCR.

    Ordinary OCR returns characters. Formula OCR needs layout. This function first
    creates candidate formula regions using dark-pixel row density, then asks the
    selected OCR engine to return structured LaTeX for each region.
    """
    regions = detect_formula_regions(image_path)
    results: list[dict[str, Any]] = []

    for idx, region in enumerate(regions, start=1):
        crop_path = _save_crop(image_path, region["bbox"])
        try:
            rec = recognize_formula_image(
                crop_path,
                engine=engine,
                prompt=STRUCTURED_FORMULA_PROMPT,
            )
            raw_latex = rec.get("latex") or rec.get("raw_text") or ""
            repaired = repair_formula_latex(raw_latex)
            diagnostics = formula_diagnostics(repaired)
            score = structure_quality_score(repaired) + float(region.get("score", 0.0))
            if not repaired:
                continue
            results.append(
                {
                    "formula_seq": idx,
                    "formula_title": "수식 영역 구조 OCR 후보" if region["kind"] != "full" else "전체 이미지 구조 OCR 후보",
                    "raw_text": rec.get("raw_text") or raw_latex,
                    "latex": repaired,
                    "normalized_latex": repaired,
                    "confidence": min(0.95, max(float(rec.get("confidence", 0.0)), 0.50) + min(score, 10.0) / 100.0),
                    "source_type": f"structured_region_{rec.get('engine', 'unknown')}",
                    "status": "CANDIDATE" if not diagnostics["warnings"] else "NEEDS_REVIEW",
                    "bbox": region["bbox"],
                    "diagnostics": diagnostics,
                }
            )
        finally:
            try:
                os.remove(crop_path)
            except OSError:
                pass

    # Deduplicate very similar candidates and keep the best-scored first.
    results.sort(key=lambda r: structure_quality_score(r.get("latex", "")), reverse=True)
    return _dedupe_results(results)


def detect_formula_regions(image_path: str) -> list[dict[str, Any]]:
    """Detect likely formula bands without heavy CV dependencies.

    It always includes the full image as fallback, then adds horizontal bands that
    have enough dark pixels. This helps Qwen/pix2tex focus on a formula instead
    of reading the entire document page as general text.
    """
    img = Image.open(image_path)
    width, height = img.size
    regions = [
        {
            "kind": "full",
            "bbox": [0, 0, width, height],
            "score": 0.0,
        }
    ]

    if width < 80 or height < 40:
        return regions

    gray = ImageOps.grayscale(img)
    # Downscale large pages for row-density analysis only.
    max_width = 1200
    scale = 1.0
    if width > max_width:
        scale = max_width / width
        gray = gray.resize((int(width * scale), int(height * scale)))
    small_w, small_h = gray.size
    pixels = gray.load()

    row_density: list[float] = []
    for y in range(small_h):
        dark = 0
        for x in range(small_w):
            if pixels[x, y] < 185:
                dark += 1
        row_density.append(dark / max(small_w, 1))

    threshold = max(0.012, min(0.08, sum(row_density) / max(len(row_density), 1) * 1.8))
    bands: list[tuple[int, int, float]] = []
    in_band = False
    start = 0
    score = 0.0
    quiet_rows = 0

    for y, density in enumerate(row_density):
        active = density >= threshold
        if active and not in_band:
            in_band = True
            start = y
            score = density
            quiet_rows = 0
        elif active and in_band:
            score += density
            quiet_rows = 0
        elif in_band:
            quiet_rows += 1
            if quiet_rows >= 5:
                end = y - quiet_rows + 1
                if end - start >= 16:
                    bands.append((start, end, score))
                in_band = False
                quiet_rows = 0
    if in_band and small_h - start >= 16:
        bands.append((start, small_h - 1, score))

    # Merge close bands to recover multi-line formulas.
    merged: list[list[float]] = []
    for y1, y2, sc in bands:
        if merged and y1 - merged[-1][1] <= 18:
            merged[-1][1] = y2
            merged[-1][2] += sc
        else:
            merged.append([float(y1), float(y2), float(sc)])

    inv_scale = 1.0 / scale
    for y1, y2, sc in merged:
        top = max(0, int((y1 - 12) * inv_scale))
        bottom = min(height, int((y2 + 12) * inv_scale))
        if bottom - top < 30:
            continue
        # Keep near-full width because actuarial formulas often span across the page.
        regions.append(
            {
                "kind": "row_band",
                "bbox": [0, top, width, bottom],
                "score": float(sc),
            }
        )

    # Avoid too many OCR calls on scanned pages.
    return regions[: int(os.environ.get("FORMULA_REGION_MAX", "6"))]


def _save_crop(image_path: str, bbox: list[int]) -> str:
    img = Image.open(image_path).convert("RGB")
    x1, y1, x2, y2 = bbox
    crop = img.crop((x1, y1, x2, y2))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        crop.save(tmp.name)
        return tmp.name


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in results:
        key = _simple_key(item.get("latex", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        item["formula_seq"] = len(deduped) + 1
        deduped.append(item)
    return deduped


def _simple_key(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())[:120]

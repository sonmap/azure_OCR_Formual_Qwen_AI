import os
import re
import tempfile
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract


def recognize_formula_image(image_path: str, engine: str | None = None, prompt: str | None = None) -> dict[str, Any]:
    """Formula image recognition adapter.

    The important difference from general OCR is that formula OCR must preserve
    two-dimensional structure: fractions, superscripts, subscripts, summation
    bounds, integral bounds, and root spans. The optional prompt is passed to
    vision LLM engines such as Qwen so that they return LaTeX instead of plain
    text lines.
    """
    selected_engine = (engine or os.environ.get("FORMULA_OCR_ENGINE") or "").strip().lower()
    if selected_engine in {"azure", "azure-ai", "azure-vision"}:
        try:
            return _recognize_with_azure_ai(image_path)
        except Exception:
            pass

    url = _resolve_formula_ocr_url(engine)
    if url:
        try:
            with open(image_path, "rb") as f:
                files = {"file": f}
                data = {}
                if prompt and selected_engine in {"qwen", "qwen-vl", "qwen2-vl"}:
                    data["prompt"] = prompt
                r = requests.post(
                    url,
                    files=files,
                    data=data,
                    timeout=float(os.environ.get("FORMULA_OCR_TIMEOUT", "480")),
                )
            r.raise_for_status()
            payload = r.json()
            latex = (payload.get("latex") or payload.get("text") or "").strip()
            raw_text = (payload.get("raw_text") or payload.get("text") or latex).strip()
            if not latex and not raw_text:
                raise RuntimeError("external formula OCR returned an empty result")
            return {
                "latex": latex,
                "raw_text": raw_text,
                "confidence": float(payload.get("confidence", 0.90)),
                "engine": payload.get("engine") or "external_formula_ocr",
                "status": "CANDIDATE",
            }
        except Exception:
            # Keep uploads usable while the external model is unavailable.
            # The local fallback below is lower quality but avoids dropping OCR entirely.
            pass

    try:
        from pix2tex.cli import LatexOCR  # type: ignore

        model = LatexOCR()
        latex = model(Image.open(image_path))
        return {
            "latex": latex,
            "raw_text": latex,
            "confidence": 0.80,
            "engine": "pix2tex",
            "status": "CANDIDATE",
        }
    except Exception:
        pass

    try:
        raw, confidence, engine = _recognize_with_tesseract_fallback(image_path)
        return {
            "latex": raw,
            "raw_text": raw,
            "confidence": confidence,
            "engine": engine,
            "status": "NEEDS_REVIEW",
        }
    except Exception as e:
        return {"latex": "", "raw_text": str(e), "confidence": 0.0, "engine": "tesseract_fallback", "status": "ERROR"}


def _resolve_formula_ocr_url(engine: str | None) -> str | None:
    selected = (engine or os.environ.get("FORMULA_OCR_ENGINE") or "").strip().lower()
    if selected in {"qwen", "qwen-vl", "qwen2-vl"}:
        return os.environ.get("FORMULA_OCR_URL_QWEN") or "http://qwen-vl:9100/predict"
    if selected in {"formula", "formula-ocr", "pix2tex"}:
        return os.environ.get("FORMULA_OCR_URL_FORMULA") or "http://formula-ocr:9000/predict"
    if selected in {"azure", "azure-ai", "azure-vision"}:
        return None
    return os.environ.get("FORMULA_OCR_URL")


def _recognize_with_azure_ai(image_path: str) -> dict[str, Any]:
    endpoint = (os.environ.get("AZURE_AI_ENDPOINT") or "").rstrip("/")
    key = os.environ.get("AZURE_AI_KEY") or ""
    if not endpoint or not key:
        raise RuntimeError("AZURE_AI_ENDPOINT/AZURE_AI_KEY is not configured")

    analyze_url = f"{endpoint}/vision/v3.2/read/analyze"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/octet-stream",
    }
    with open(image_path, "rb") as f:
        response = requests.post(analyze_url, headers=headers, data=f, timeout=60)
    response.raise_for_status()
    operation_url = response.headers.get("Operation-Location")
    if not operation_url:
        raise RuntimeError("Azure AI did not return Operation-Location")

    result = None
    for _ in range(30):
        poll = requests.get(operation_url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=30)
        poll.raise_for_status()
        result = poll.json()
        status = (result.get("status") or "").lower()
        if status in {"succeeded", "failed"}:
            break
        import time
        time.sleep(1)

    if not result or (result.get("status") or "").lower() != "succeeded":
        raise RuntimeError(f"Azure AI OCR failed or timed out: {result}")

    lines: list[str] = []
    for page in result.get("analyzeResult", {}).get("readResults", []):
        for line in page.get("lines", []):
            text = (line.get("text") or "").strip()
            if text:
                lines.append(text)
    raw = "\n".join(lines).strip()
    if not raw:
        raise RuntimeError("Azure AI OCR returned empty text")
    return {
        "latex": raw,
        "raw_text": raw,
        "confidence": 0.65,
        "engine": "azure-ai-vision-read",
        "status": "NEEDS_REVIEW",
    }


def _recognize_with_tesseract_fallback(image_path: str) -> tuple[str, float, str]:
    """Run several lightweight OCR passes and keep the most formula-like result."""
    lang = os.environ.get("TESSERACT_LANG", "kor+eng")
    img = Image.open(image_path)
    variants = _prepare_ocr_variants(img)
    configs = [
        "--oem 3 --psm 6 -c preserve_interword_spaces=1",
        "--oem 3 --psm 11 -c preserve_interword_spaces=1",
        "--oem 3 --psm 4 -c preserve_interword_spaces=1",
        "--oem 3 --psm 7 -c preserve_interword_spaces=1",
    ]

    best_text = ""
    best_score = -1.0
    best_conf = 0.0
    with tempfile.TemporaryDirectory() as tmpdir:
        for variant_no, variant in enumerate(variants, start=1):
            temp_path = Path(tmpdir) / f"formula_ocr_{variant_no}.png"
            variant.save(temp_path)
            for config in configs:
                text = pytesseract.image_to_string(variant, lang=lang, config=config)
                text = _clean_ocr_text(text)
                score = _score_formula_ocr_text(text)
                if score > best_score:
                    best_text = text
                    best_score = score
                    best_conf = _confidence_from_score(score)

    return best_text, best_conf, "tesseract_preprocessed"


def _prepare_ocr_variants(img: Image.Image) -> list[Image.Image]:
    """Create OCR-friendly variants for scanned formulas without extra dependencies."""
    gray = ImageOps.grayscale(img)
    gray = ImageOps.exif_transpose(gray)

    variants: list[Image.Image] = []
    for scale in [2, 3, 4]:
        enlarged = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
        enhanced = ImageEnhance.Contrast(enlarged).enhance(1.8)
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.5)
        variants.append(enhanced)

        threshold = _auto_threshold(enhanced)
        binary = enhanced.point(lambda p, t=threshold: 255 if p > t else 0)
        variants.append(binary)

        denoised = binary.filter(ImageFilter.MedianFilter(size=3))
        variants.append(denoised)

    return variants


def _auto_threshold(img: Image.Image) -> int:
    hist = img.histogram()
    total = sum(hist)
    if total == 0:
        return 180
    weighted_sum = sum(i * count for i, count in enumerate(hist))
    background_mean = weighted_sum / total
    return max(130, min(210, int(background_mean * 0.88)))


def _clean_ocr_text(text: str) -> str:
    s = (text or "").replace("\r", "\n")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = s.replace("×", " × ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _score_formula_ocr_text(text: str) -> float:
    if not text:
        return -1.0
    score = 0.0
    score += min(len(text), 500) / 80.0
    score += 4.0 * len(re.findall(r"[=+\-*/×/^_]", text))
    score += 3.0 * len(re.findall(r"\b(?:E|A|D|N|M|l|q|v|APV|ANC|FNC)\b", text, flags=re.IGNORECASE))
    score += 2.0 * len(re.findall(r"[(){}]", text))
    score += 2.0 * len(re.findall(r"\b[tx]\s*[+-]\s*\d+\b", text))
    score -= 1.5 * len(re.findall(r"[?□■●◆]", text))
    return score


def _confidence_from_score(score: float) -> float:
    if score <= 0:
        return 0.20
    if score >= 45:
        return 0.55
    return round(0.25 + min(score, 45) / 150.0, 2)

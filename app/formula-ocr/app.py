import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image

app = FastAPI(title="Formula OCR API", version="0.1.0")

_model_lock = threading.Lock()
_model: Any | None = None
_model_error: str | None = None


@app.get("/health")
def health() -> dict[str, str | bool | None]:
    return {
        "status": "OK",
        "service": "formula-ocr-api",
        "engine": "pix2tex",
        "model_loaded": _model is not None,
        "model_error": _model_error,
    }


@app.post("/predict")
async def predict(file: UploadFile = File(...)) -> dict[str, Any]:
    model = _load_pix2tex_model()
    suffix = Path(file.filename or "formula.png").suffix or ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        image = Image.open(tmp_path)
        latex = model(image)
        latex = (latex or "").strip()
        return {
            "latex": latex,
            "text": latex,
            "raw_text": latex,
            "confidence": 0.82 if latex else 0.0,
            "engine": "pix2tex",
        }
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _load_pix2tex_model() -> Any:
    global _model, _model_error
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model
        try:
            from pix2tex.cli import LatexOCR  # type: ignore

            _model = LatexOCR()
            _model_error = None
            return _model
        except Exception as exc:
            _model_error = str(exc)
            raise HTTPException(status_code=503, detail=f"pix2tex model is not available: {_model_error}")

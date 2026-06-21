import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image
from transformers import AutoProcessor

try:
    from transformers import Qwen2VLForConditionalGeneration
except Exception:  # pragma: no cover
    Qwen2VLForConditionalGeneration = None

try:
    from transformers import Qwen2_5_VLForConditionalGeneration
except Exception:  # pragma: no cover
    Qwen2_5_VLForConditionalGeneration = None

try:
    from qwen_vl_utils import process_vision_info
except Exception:  # pragma: no cover
    process_vision_info = None

MODEL_ID = os.environ.get("QWEN_MODEL_ID", "Qwen/Qwen2-VL-2B-Instruct")
MAX_NEW_TOKENS = int(os.environ.get("QWEN_MAX_NEW_TOKENS", "256"))

app = FastAPI(title="Qwen VL OCR API", version="0.1.0")

_lock = threading.Lock()
_model: Any | None = None
_processor: Any | None = None
_model_error: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "OK",
        "service": "qwen-vl-api",
        "model_id": MODEL_ID,
        "model_loaded": _model is not None,
        "model_error": _model_error,
        "torch_num_threads": torch.get_num_threads(),
    }


@app.post("/predict")
async def predict(
    file: UploadFile = File(...),
    prompt: str = Form(
        "Read the actuarial formula in this image. Return only LaTeX. "
        "Preserve actuarial symbols such as \\ddot{a}_x, l_x, N_x, D_x, "
        "\\sum, e^{-\\alpha j}, and select-period notation."
    ),
) -> dict[str, Any]:
    model, processor = _load_model()
    suffix = Path(file.filename or "image.png").suffix or ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        image_path = tmp.name

    try:
        image = Image.open(image_path).convert("RGB")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        if process_vision_info is None:
            image_inputs, video_inputs = [image], None
        else:
            image_inputs, video_inputs = process_vision_info(messages)
        inputs = processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )
        with torch.inference_mode():
            generated_ids = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS)
        trimmed = [
            out_ids[len(in_ids):]
            for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        output = processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0].strip()
        return {
            "latex": output,
            "text": output,
            "raw_text": output,
            "confidence": 0.70 if output else 0.0,
            "engine": "qwen-vl",
            "model_id": MODEL_ID,
        }
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass


def _load_model() -> tuple[Any, Any]:
    global _model, _processor, _model_error
    if _model is not None and _processor is not None:
        return _model, _processor
    with _lock:
        if _model is not None and _processor is not None:
            return _model, _processor
        try:
            torch.set_num_threads(int(os.environ.get("TORCH_NUM_THREADS", "2")))
            _processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
            model_cls = _select_model_class(MODEL_ID)
            _model = model_cls.from_pretrained(
                MODEL_ID,
                torch_dtype=torch.float32,
                device_map="cpu",
                low_cpu_mem_usage=True,
                trust_remote_code=True,
            )
            _model.eval()
            _model_error = None
            return _model, _processor
        except Exception as exc:
            _model_error = str(exc)
            raise HTTPException(status_code=503, detail=f"Qwen2.5-VL model is not available: {_model_error}")


def _select_model_class(model_id: str) -> Any:
    if "Qwen2.5-VL" in model_id:
        if Qwen2_5_VLForConditionalGeneration is None:
            raise RuntimeError("Installed transformers does not provide Qwen2_5_VLForConditionalGeneration")
        return Qwen2_5_VLForConditionalGeneration
    if "Qwen2-VL" in model_id:
        if Qwen2VLForConditionalGeneration is None:
            raise RuntimeError("Installed transformers does not provide Qwen2VLForConditionalGeneration")
        return Qwen2VLForConditionalGeneration
    raise RuntimeError(f"Unsupported Qwen VL model id: {model_id}")

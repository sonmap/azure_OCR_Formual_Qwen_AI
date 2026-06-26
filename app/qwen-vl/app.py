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
MAX_NEW_TOKENS = int(os.environ.get("QWEN_MAX_NEW_TOKENS", "512"))

DEFAULT_FORMULA_PROMPT = """
You are a mathematical OCR engine for actuarial formulas.
Return ONLY LaTeX. Do not explain. Do not output Markdown.
Preserve all two-dimensional math structure:
- fractions as \\frac{numerator}{denominator}
- summation as \\sum_{lower}^{upper}
- integral as \\int_{lower}^{upper}
- superscripts with ^{...}
- subscripts with _{...}
- roots as \\sqrt{...}
- actuarial symbols such as l_x, q_x, p_x, v^t, D_x, N_x, A_x, \\ddot{a}_x
- shifted ages such as l_{x+1}, q_{x+t}, E_{ANC}(t,x)
If the image has multiple broken OCR lines, merge them into one valid LaTeX formula.
""".strip()

app = FastAPI(title="Qwen VL OCR API", version="0.2.0")

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
    prompt: str = Form(DEFAULT_FORMULA_PROMPT),
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
        output = _strip_markdown(output)
        return {
            "latex": output,
            "text": output,
            "raw_text": output,
            "confidence": 0.72 if output else 0.0,
            "engine": "qwen-vl-structured-latex",
            "model_id": MODEL_ID,
        }
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass


def _strip_markdown(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
    if s.endswith("```"):
        s = s[:-3]
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2]
    return s.strip()


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

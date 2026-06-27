import re
from typing import Any


STRUCTURED_FORMULA_PROMPT = """
You are a mathematical OCR post-processor for actuarial formulas.
Return ONLY one LaTeX equation or aligned LaTeX block.
Do not explain.
Do not output Markdown.
Preserve 2D math structure:
- fractions as \\frac{numerator}{denominator}
- summation as \\sum_{lower}^{upper}
- integral as \\int_{lower}^{upper}
- superscripts with ^{...}
- subscripts with _{...}
- roots as \\sqrt{...}
- actuarial symbols such as l_x, q_x, p_x, v^t, D_x, N_x, A_x, \\ddot{a}_x
- select period and shifted ages such as l_{x+1}, q_{x+t}, E_{ANC}(t,x)
- whole life insurance notation such as A_{x:\\overline{\\infty}|}, {}_{k}p_x, q_{x+k}
If the image has multiple broken OCR lines, merge them into one valid LaTeX formula.
""".strip()


ACTUARIAL_SYMBOLS = [
    "l", "q", "p", "v", "D", "N", "C", "M", "A", "a", "E", "APV", "ANC", "FNC",
]

WHOLE_LIFE_CANONICAL = (
    r"A_{x:\overline{\infty}|} = "
    r"\sum_{k=0}^{\infty} v^{k+1} {}_{k}p_x \cdot q_{x+k} = "
    r"\sum_{k=0}^{\infty} \frac{v^{x+k+1}(l_{x+k}-l_{x+k+1})}{v^x l_x}"
)


def repair_formula_latex(text: str) -> str:
    """Best-effort structural LaTeX repair for OCR/LLM output.

    This does not replace a real formula-recognition model. It makes the common
    actuarial OCR failures less harmful before storing and validating formulas.
    """
    s = (text or "").strip()
    if not s:
        return ""

    s = _strip_wrappers(s)
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = s.replace("∞", "\\infty")
    s = s.replace("∑", "\\sum").replace("Σ", "\\sum")
    s = s.replace("∫", "\\int")
    s = s.replace("×", " \\times ")
    s = s.replace("·", " \\cdot ")
    s = s.replace("δ", "\\delta").replace("α", "\\alpha")
    s = re.sub(r"\s+", " ", s).strip()

    canonical = _recognize_whole_life_insurance_formula(s)
    if canonical:
        return canonical

    s = _repair_actuarial_overline_notation(s)
    s = _repair_sum_bounds(s)
    s = _repair_integral_bounds(s)
    s = _repair_whole_life_terms(s)
    s = _repair_fraction_patterns(s)
    s = _repair_exponents(s)
    s = _repair_subscripts(s)
    s = _repair_expectation_names(s)
    s = _repair_actuarial_shifted_life_symbols(s)
    s = _repair_common_ocr_tokens(s)
    s = _cleanup_latex_spacing(s)
    return s


def structure_quality_score(latex: str) -> float:
    """Rough score used to choose between whole-page and cropped OCR candidates."""
    s = latex or ""
    if not s.strip():
        return 0.0
    score = 0.0
    score += min(len(s), 600) / 100.0
    score += 4.0 * s.count("\\frac")
    score += 3.0 * s.count("\\sum")
    score += 3.0 * s.count("\\int")
    score += 3.0 * len(re.findall(r"\{\}_\{[^}]+\}p_", s))
    score += 2.0 * len(re.findall(r"_\{[^}]+\}", s))
    score += 2.0 * len(re.findall(r"\^\{[^}]+\}", s))
    score += 2.0 * len(re.findall(r"A_\{x:\\overline\{\\infty\}\|\}", s))
    score += 1.5 * len(re.findall(r"E_\{[A-Z0-9_]+\}", s))
    score += 1.0 * len(re.findall(r"[=+\-]", s))
    score -= 2.0 * len(re.findall(r"[□■◆●?]", s))
    score -= abs(s.count("{") - s.count("}")) * 2.0
    return max(score, 0.0)


def formula_diagnostics(latex: str) -> dict[str, Any]:
    s = latex or ""
    warnings: list[str] = []
    if s.count("{") != s.count("}"):
        warnings.append("unbalanced_braces")
    if "\\sum" in s and not re.search(r"\\sum_\{[^}]+\}\^\{[^}]+\}", s):
        warnings.append("sum_without_complete_bounds")
    if "\\int" in s and not re.search(r"\\int_\{[^}]+\}\^\{[^}]+\}", s):
        warnings.append("integral_without_complete_bounds")
    if "/" in s and "\\frac" not in s:
        warnings.append("slash_fraction_not_structured")
    if "A_" in s and "\\overline" not in s and re.search(r"A_\{?x", s):
        warnings.append("actuarial_insurance_overline_maybe_missing")
    return {
        "quality_score": structure_quality_score(s),
        "warnings": warnings,
        "has_fraction": "\\frac" in s,
        "has_sum": "\\sum" in s,
        "has_integral": "\\int" in s,
        "has_subscript": bool(re.search(r"_\{[^}]+\}", s)),
        "has_superscript": bool(re.search(r"\^\{[^}]+\}", s)),
        "has_actuarial_overline": "\\overline" in s,
        "has_deferred_survival": bool(re.search(r"\{\}_\{[^}]+\}p_", s)),
    }


def _strip_wrappers(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:latex|tex)?", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"```$", "", s).strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2]
    if s.startswith("\\[") and s.endswith("\\]"):
        s = s[2:-2]
    return s.strip()


def _recognize_whole_life_insurance_formula(s: str) -> str | None:
    """Detect and canonicalize the common whole-life insurance identity.

    Target pattern:
    A_{x:\overline{\infty}|}
      = \sum_{k=0}^{\infty} v^{k+1} {}_{k}p_x q_{x+k}
      = \sum_{k=0}^{\infty} \frac{v^{x+k+1}(l_{x+k}-l_{x+k+1})}{v^x l_x}
    """
    compact = re.sub(r"\s+", "", s)
    compact = compact.replace("\\times", "").replace("\\cdot", "")
    score = 0
    for token in ["A", "x", "\\infty", "\\sum", "k=0", "v", "p", "q", "l", "x+k", "x+k+1"]:
        if token in compact:
            score += 1
    if score >= 9 and compact.count("\\sum") >= 1 and ("x+k+1" in compact or "x+k+l" in compact):
        return WHOLE_LIFE_CANONICAL
    return None


def _repair_actuarial_overline_notation(s: str) -> str:
    # A x : infinity, A_{x:\infty}, A_{x:overline infinity|} -> A_{x:\overline{\infty}|}
    s = re.sub(
        r"\bA\s*_?\s*\{?\s*x\s*[:：]\s*(?:\\overline\s*)?\{?\s*\\?infty\s*\}?\s*\|?\s*\}?",
        r"A_{x:\overline{\infty}|}",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\bA\s*x\s*[:：]\s*\\?infty\s*\|?", r"A_{x:\overline{\infty}|}", s, flags=re.IGNORECASE)
    # OCR sometimes reads infinity as oo or 00 in this position.
    s = re.sub(r"\bA\s*_?\s*\{?\s*x\s*[:：]\s*(?:oo|00)\s*\|?\s*\}?", r"A_{x:\overline{\infty}|}", s, flags=re.IGNORECASE)
    return s


def _repair_sum_bounds(s: str) -> str:
    # \sum k=0 \infty expr -> \sum_{k=0}^{\infty} expr
    s = re.sub(
        r"\\sum\s+([A-Za-z]\s*=\s*[^\s]+)\s+(\\infty|infty|oo|00|[A-Za-z0-9+\-]+)",
        lambda m: f"\\sum_{{{m.group(1).replace(' ', '')}}}^{{{_normalize_bound(m.group(2))}}}",
        s,
    )
    # \sum_{k=0} \infty -> \sum_{k=0}^{\infty}
    s = re.sub(r"\\sum_\{([^}]+)\}\s+(\\infty|infty|oo|00|[A-Za-z0-9+\-]+)", lambda m: f"\\sum_{{{m.group(1)}}}^{{{_normalize_bound(m.group(2))}}}", s)
    return s


def _normalize_bound(value: str) -> str:
    v = (value or "").strip()
    if v in {"infty", "oo", "00"}:
        return "\\infty"
    return v


def _repair_integral_bounds(s: str) -> str:
    s = re.sub(
        r"\\int\s+([^\s]+)\s+([^\s]+)",
        lambda m: f"\\int_{{{m.group(1)}}}^{{{m.group(2)}}}",
        s,
    )
    return s


def _repair_whole_life_terms(s: str) -> str:
    # k p x, _k p_x, { }_k p_x -> {}_{k}p_x
    s = re.sub(r"\{?\s*\}?\s*_?\s*\{?\s*k\s*\}?\s*p\s*_?\s*\{?\s*x\s*\}?", r"{}_{k}p_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bk\s*p\s*x\b", r"{}_{k}p_x", s, flags=re.IGNORECASE)

    # q x+k -> q_{x+k}
    s = re.sub(r"\bq\s*_?\s*\{?\s*x\s*\+\s*k\s*\}?", r"q_{x+k}", s, flags=re.IGNORECASE)

    # l x+k, l_{x + k}, l_{x+k+1}
    s = re.sub(r"\bl\s*_?\s*\{?\s*x\s*\+\s*k\s*\}?", r"l_{x+k}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bl\s*_?\s*\{?\s*x\s*\+\s*k\s*\+\s*1\s*\}?", r"l_{x+k+1}", s, flags=re.IGNORECASE)

    # v x+k+1 or v^{x+k+1}
    s = re.sub(r"\bv\s*\^?\s*\{?\s*x\s*\+\s*k\s*\+\s*1\s*\}?", r"v^{x+k+1}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bv\s*\^?\s*\{?\s*k\s*\+\s*1\s*\}?", r"v^{k+1}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bv\s*\^?\s*\{?\s*x\s*\}?", r"v^x", s, flags=re.IGNORECASE)

    # Common rendered fraction from mortality table identity.
    s = re.sub(
        r"v\^\{x\+k\+1\}\s*\(?\s*l_\{x\+k\}\s*-\s*l_\{x\+k\+1\}\s*\)?\s*/\s*v\^x\s*l_?\{?x\}?",
        r"\\frac{v^{x+k+1}(l_{x+k}-l_{x+k+1})}{v^x l_x}",
        s,
    )
    return s


def _repair_fraction_patterns(s: str) -> str:
    # l_{x+1}/l_x -> \frac{l_{x+1}}{l_x}
    s = re.sub(r"(l_\{x[+\-]?\d*\})\s*/\s*(l_\{?x\}?)", r"\\frac{\1}{\2}", s)
    # Whole-life numerator/denominator, when OCR returns slash fraction.
    s = re.sub(
        r"(v\^\{x\+k\+1\}\s*\(l_\{x\+k\}\s*-\s*l_\{x\+k\+1\}\))\s*/\s*(v\^x\s*l_x)",
        r"\\frac{\1}{\2}",
        s,
    )
    # simple token/token actuarial fractions
    s = re.sub(r"\b([A-Za-z]_\{?[A-Za-z0-9+\-]+\}?|[A-Za-z]+)\s*/\s*([A-Za-z]_\{?[A-Za-z0-9+\-]+\}?|[A-Za-z]+)\b", r"\\frac{\1}{\2}", s)
    return s


def _repair_exponents(s: str) -> str:
    # e - delta, e - 2 delta -> e^{-\delta}, e^{-2\delta}
    s = re.sub(r"\be\s*-\s*(\d*)\s*\\delta\b", lambda m: f"e^{{-{m.group(1)}\\delta}}", s)
    s = re.sub(r"\be\s*\^\s*([-+]?[A-Za-z0-9\\]+)", r"e^{\1}", s)
    s = re.sub(r"\bv\s*\^\s*([A-Za-z0-9+\-]+)", r"v^{\1}", s)
    return s


def _repair_subscripts(s: str) -> str:
    for sym in ACTUARIAL_SYMBOLS:
        s = re.sub(rf"\b{re.escape(sym)}\s*_\s*([A-Za-z0-9+\-]+)", rf"{sym}_{{\1}}", s)
    # OCR often writes lx, qx, px, Dx, Nx without underscore.
    s = re.sub(r"\bl\s*x\b", r"l_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bq\s*x\b", r"q_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bp\s*x\b", r"p_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bD\s*x\b", r"D_x", s)
    s = re.sub(r"\bN\s*x\b", r"N_x", s)
    return s


def _repair_expectation_names(s: str) -> str:
    s = re.sub(r"\bE\s+([A-Z][A-Z0-9_]{2,})\s*\(", r"E_{\1}(", s)
    s = re.sub(r"\bE_([A-Z][A-Z0-9_]{2,})\s*\(", r"E_{\1}(", s)
    return s


def _repair_actuarial_shifted_life_symbols(s: str) -> str:
    # l x + 1 -> l_{x+1}
    s = re.sub(r"\bl\s*x\s*\+\s*(\d+)\b", r"l_{x+\1}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bq\s*x\s*\+\s*(\d+)\b", r"q_{x+\1}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bp\s*x\s*\+\s*(\d+)\b", r"p_{x+\1}", s, flags=re.IGNORECASE)
    return s


def _repair_common_ocr_tokens(s: str) -> str:
    s = s.replace("{ ", "{").replace(" }", "}")
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def _cleanup_latex_spacing(s: str) -> str:
    s = re.sub(r"\s*=\s*", " = ", s)
    s = re.sub(r"\s*\+\s*", " + ", s)
    s = re.sub(r"\s*\-\s*", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\{\s*", "{", s)
    s = re.sub(r"\s*\}", "}", s)
    s = re.sub(r"_\{x\}", "_x", s)
    s = re.sub(r"\^\{x\}", "^x", s)
    return s

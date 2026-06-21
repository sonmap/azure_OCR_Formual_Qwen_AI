import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

LATEX_BLOCK_RE = re.compile(r"\$\$(.*?)\$\$|\\\[(.*?)\\\]", re.DOTALL)

# 보험계리/수리 공식에서 자주 나오는 기호 후보. 계속 확장하면 됩니다.
SYMBOL_ALIASES = {
    "예상 사망자 수": "expected_deaths",
    "예상 총 지급액": "expected_total_claim",
    "1인당 순보험료": "net_premium_per_person",
    "총 지급액": "total_claim",
    "총 가입자 수": "policy_count",
    "보장 금액": "sum_assured",
    "사망률": "mortality_rate",
    "q_x": "qx",
    "l_x": "lx",
    "v": "discount_factor",
    "D_x": "Dx",
    "N_x": "Nx",
    "A_x": "Ax",
    "IA_x": "IAx",
    "a_x": "ax",
    "delta": "delta",
    "δ": "delta",
    "α": "alpha",
    "\\alpha": "alpha",
    "\\ddot{a}": "annuity_due",
    "N_x": "Nx",
    "D_x": "Dx",
}

MATH_LINE_TOKENS = [
    "=", "+", "-", "×", "*", "/", "^", "_", "∫", "Σ", "∑", "δ", "α", "ä", "\\frac", "\\int", "\\sum",
]
FORMULA_WORD_RE = re.compile(
    r"(E\s*[A-Z](?:\s*[A-Z]){1,}|[A-Z]_[A-Z]+|[A-Z]{2,}\s*\(|[A-Za-z]+\s*\([^)]*\)|I\s*A\s*x|l\s*x|q\s*x|p\s*x|N\s*x|D\s*x|e\s*-|APV|ANC|FNC|AEB|APV|EB|ä\s*x|n\s*E\s*x|S\s*x)",
    re.IGNORECASE,
)
OPERATOR_ONLY_RE = re.compile(r"^(?:⇒|=|\+|\-|×|\*)+$")


def clean_latex(text: str) -> str:
    s = text.strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2]
    s = s.replace("\u200b", "")
    s = re.sub(r"\s+", " ", s).strip()
    s = _normalize_insurance_actuarial_sum_notation(s)
    return _cleanup_actuarial_latex_spacing(s)


def extract_formula_candidates(text: str) -> list[dict[str, Any]]:
    """텍스트에서 LaTeX 블록 또는 수식처럼 보이는 라인을 추출합니다.

    v0.3 개선점:
    - OCR이 수식을 여러 줄로 쪼개는 경우, 연속된 수식 라인을 하나의 공식 후보로 묶습니다.
    - 예: E APVFNC(t,x) / = / E ANC(t,x) / + / ... 를 하나의 formula_block으로 저장합니다.
    """
    candidates: list[dict[str, Any]] = []
    seq = 1

    # 1) $$...$$ 또는 \[...\]는 가장 신뢰도 높게 우선 추출
    for m in LATEX_BLOCK_RE.finditer(text or ""):
        latex = clean_latex(m.group(1) or m.group(2) or "")
        if latex:
            title = guess_title(text[: m.start()])
            candidates.append(
                {
                    "formula_seq": seq,
                    "formula_title": title,
                    "raw_text": m.group(0),
                    "latex": latex,
                    "normalized_latex": normalize_latex(latex),
                    "source_type": "text_latex_block",
                    "confidence": 0.95,
                }
            )
            seq += 1

    if candidates:
        return candidates

    # 2) OCR 줄 깨짐 보정: 수식 라인 그룹핑
    groups = group_broken_formula_lines(text or "")
    used_lines: set[int] = set()
    for g in groups:
        raw = "\n".join(g["lines"])
        latex = normalize_broken_formula_text(raw)
        if len(latex) < 5:
            continue
        for i in g["line_indexes"]:
            used_lines.add(i)
        candidates.append(
            {
                "formula_seq": seq,
                "formula_title": guess_title((text or "")[: g.get("start_offset", 0)]) or "멀티라인 계리식 후보",
                "raw_text": raw,
                "latex": latex,
                "normalized_latex": normalize_latex(latex),
                "source_type": "multiline_formula_block",
                "confidence": 0.72,
            }
        )
        seq += 1

    # 3) 그룹에 포함되지 않은 단일 수식 라인 후보
    for idx, line in enumerate((text or "").splitlines()):
        if idx in used_lines:
            continue
        line = line.strip()
        if not line:
            continue
        if looks_like_formula(line):
            candidates.append(
                {
                    "formula_seq": seq,
                    "formula_title": guess_title(line),
                    "raw_text": line,
                    "latex": normalize_broken_formula_text(line),
                    "normalized_latex": normalize_latex(line),
                    "source_type": "text_formula_line",
                    "confidence": 0.65,
                }
            )
            seq += 1

    return candidates


def is_formulaish_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if OPERATOR_ONLY_RE.match(s):
        return True
    if any(t in s for t in MATH_LINE_TOKENS):
        return True
    if FORMULA_WORD_RE.search(s):
        return True
    # x+1, t+2 같은 첨자/인자 조각
    if re.search(r"\b[tx]\s*[+-]\s*\d+\b", s):
        return True
    return False


def group_broken_formula_lines(text: str) -> list[dict[str, Any]]:
    """OCR 결과에서 연속된 수식 조각 라인을 하나의 block으로 묶습니다."""
    lines = text.splitlines()
    groups: list[dict[str, Any]] = []
    current: list[str] = []
    current_idx: list[int] = []
    offset = 0
    start_offset = 0

    def flush() -> None:
        nonlocal current, current_idx, start_offset
        if len(current) >= 2 or (current and looks_like_formula(current[0])):
            joined = " ".join(current)
            # 등호나 +가 하나 이상 있어야 실제 공식 후보로 저장
            if any(tok in joined for tok in ["=", "+", "×", "*", "/"]):
                groups.append({"lines": current[:], "line_indexes": current_idx[:], "start_offset": start_offset})
        current = []
        current_idx = []

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        formulaish = is_formulaish_line(line)
        # OCR이 함수 인자를 줄바꿈하는 경우: E_ANC(t, / x) 를 같은 수식으로 유지
        if current and not formulaish and re.match(r"^[A-Za-z0-9_,+\-\s\)\(]+$", line) and len(line) <= 30:
            formulaish = True
        # 긴 한국어 설명 문장은 수식 그룹을 끊는다. 단, 짧은 title은 제외.
        korean_alpha_count = len(re.findall(r"[가-힣]", line))
        is_long_prose = korean_alpha_count >= 12 and not any(t in line for t in MATH_LINE_TOKENS)

        if formulaish and not is_long_prose:
            if not current:
                start_offset = offset
            current.append(line)
            current_idx.append(idx)
        else:
            flush()
        offset += len(raw_line) + 1
    flush()
    return groups


def looks_like_formula(line: str) -> bool:
    return is_formulaish_line(line) and len(line.strip()) > 2


def guess_title(context: str) -> str | None:
    if not context:
        return None
    # 마지막 문장/콜론 앞 문구를 제목 후보로 사용
    tail = context[-180:].replace("\n", " ")
    m = re.search(r"([가-힣A-Za-z0-9 /()_-]{3,100})\s*[:：]", tail)
    if m:
        return m.group(1).strip()
    return None


def normalize_broken_formula_text(raw: str) -> str:
    """여러 줄로 깨진 OCR 수식을 사람이 읽기 쉬운 1개 수식 문자열로 보정합니다.

    운영용 완성 수식 OCR이 아니라 PoC용 후처리입니다.
    계리식 사전이 늘어나면 이 함수의 치환 규칙을 확장하면 됩니다.
    """
    s = raw.replace("\r", "\n")
    # 줄바꿈을 공백으로 합치되, 연산자는 주변 공백을 정리
    s = re.sub(r"\n+", " ", s)
    s = s.replace("⇒", "").strip()
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    s = _normalize_actuarial_annuity_notation(s)
    s = _normalize_commutation_annuity_definition(s)
    s = _normalize_insurance_actuarial_sum_notation(s)

    # OCR: E APVFNC(t,x) -> E_{APVFNC}(t,x), E ANC(...) -> E_{ANC}(...)
    s = _normalize_actuarial_expectation_symbols(s)
    s = re.sub(r"\bE_([A-Z][A-Z0-9_]{2,})\s*\(", r"E_{\1}(", s)

    # OCR: e - δ, e - 2δ -> e^{-δ}, e^{-2δ}
    s = re.sub(r"\be\s*-\s*(\d*)\s*δ", lambda m: "e^{-" + (m.group(1) or "") + "δ}", s)
    s = re.sub(r"\be\s*\+\s*-\s*(\d*)\s*δ", lambda m: "e^{-" + (m.group(1) or "") + "δ}", s)
    s = re.sub(r"\be\s*δ\b", r"e^{-δ}", s)
    s = re.sub(r"\be\s*(\d+)\s*δ\b", r"e^{-\1δ}", s)
    s = re.sub(r"\be\s*-\s*(\d+)\s*delta", r"e^{-\1δ}", s, flags=re.IGNORECASE)
    s = re.sub(r"\be\s*-\s*delta", r"e^{-δ}", s, flags=re.IGNORECASE)

    # OCR: lx +1/lx, lx + 2 / lx -> l_{x+1}/l_x
    s = re.sub(r"\bl\s*x\s*\+\s*(\d+)\s*/\s*l\s*x\b", r"l_{x+\1}/l_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bl\s*x\s*/\s*l\s*x\b", r"l_x/l_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bl\s*x\b", r"l_x", s, flags=re.IGNORECASE)

    # OCR: t+1, x+1 인자 공백 정리
    s = re.sub(r"([tx])\s*\+\s*(\d+)", r"\1+\2", s)
    s = re.sub(r"([tx])\s*-\s*(\d+)", r"\1-\2", s)
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\(\s*", "(", s)
    s = re.sub(r"\s*\)", ")", s)

    # 연산자 주변 정리
    s = re.sub(r"\s*=\s*", " = ", s)
    s = re.sub(r"\s*\+\s*", " + ", s)
    s = re.sub(r"\s*×\s*", " × ", s)
    s = re.sub(r"\s*\*\s*", " * ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # 연산자 주변 공백 정리 후 함수 인자/첨자 안의 +/-는 다시 붙입니다.
    s = re.sub(r"([tx])\s*\+\s*(\d+)", r"\1+\2", s)
    s = re.sub(r"([tx])\s*-\s*(\d+)", r"\1-\2", s)
    s = re.sub(r"l_\{x\s*\+\s*(\d+)\}", r"l_{x+\1}", s)
    s = re.sub(r"e\{\s*-\s*(\d*)\s*δ\s*\}", lambda m: "e^{-" + (m.group(1) or "") + "δ}", s)
    # OCR often turns a multiplication before discount factor into bracket/equals junk.
    s = re.sub(r"\]\s*[\}\)]\s*\(\s*=\s*(e\^\{-[^}]+})", r"] × \1", s)
    s = re.sub(r"\]\s*[\}\)]\s*=\s*(e\^\{-[^}]+})", r"] × \1", s)
    # 반복 + 정리
    s = re.sub(r"\+\s*\+", "+", s)
    s = _normalize_actuarial_annuity_notation(s)
    s = _normalize_commutation_annuity_definition(s)
    s = _normalize_insurance_actuarial_sum_notation(s)
    s = _cleanup_actuarial_latex_spacing(s)
    s = _trim_dangling_formula_tail(s)
    return s


def _normalize_commutation_annuity_definition(text: str) -> str:
    """Normalize annuity-due and commutation function definitions.

    OCR often splits the summation sign into an infinity marker plus "X" and
    loses the right side of "N_x =" onto following lines.
    """
    s = text
    compact = re.sub(r"\s+", "", s)
    lower = compact.lower()

    has_annuity = (
        "\\ddot{a}_x" in s
        or re.search(r"(?:ä|¨)\s*a?\s*x", s, flags=re.IGNORECASE)
        or re.search(r"a\s*x\s*:\s*(?:∞|\\infty)", s, flags=re.IGNORECASE)
    )
    has_k_sum = (
        "k=0" in compact
        and ("∞" in s or "\\infty" in s or "infty" in lower)
        and re.search(r"v\s*(?:\^|\*)?\s*(?:k\s*\+\s*x|k\+x)", s, flags=re.IGNORECASE)
        and re.search(r"l\s*_?\{?\s*(?:k\s*\+\s*x|x\s*\+\s*k)\s*\}?", s, flags=re.IGNORECASE)
    )
    has_nx_dx = re.search(r"N\s*_?\{?\s*x\s*\}?\s*/?\s*D\s*_?\{?\s*x\s*\}?", s, flags=re.IGNORECASE)
    has_nx_definition = re.search(r"N\s*_?\{?\s*x\s*\}?\s*=", s, flags=re.IGNORECASE)
    has_y_sum = (
        ("y=x" in compact or "y=x" in lower)
        and ("∞" in s or "\\infty" in s or "infty" in lower)
        and re.search(r"v\s*(?:\^|\*)?\s*y", s, flags=re.IGNORECASE)
        and re.search(r"l\s*_?\{?\s*y\s*\}?", s, flags=re.IGNORECASE)
    )
    has_broken_y_sum = (
        has_nx_definition
        and ("y=x" in compact or "y=x" in lower)
        and ("vy" in lower or "v^y" in lower or "v*y" in lower)
        and ("ly" in lower or "l_y" in lower)
    )

    if has_annuity and (has_k_sum or has_nx_dx) and (has_y_sum or has_broken_y_sum or has_nx_definition):
        return (
            r"\ddot{a}_x = \ddot{a}_{x:\overline{\infty}|} "
            r"= \sum_{k=0}^{\infty} \frac{v^{k+x}l_{k+x}}{D_x} "
            r"= \frac{N_x}{D_x},\quad N_x = \sum_{y=x}^{\infty} v^y l_y"
        )

    if has_nx_definition and not has_annuity and (has_y_sum or has_broken_y_sum):
        return r"N_x = \sum_{y=x}^{\infty} v^y l_y"

    s = re.sub(
        r"N\s*x\s*=\s*(?:∞|\\infty|infty)\s*[Xx]\s*y\s*=\s*x\s*v\s*y\s*l\s*y",
        lambda _: r"N_x = \sum_{y=x}^{\infty} v^y l_y",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"N_x\s*=\s*(?:∞|\\infty|infty)\s*[Xx]\s*y\s*=\s*x\s*v\s*\^?\s*y\s*l_y",
        lambda _: r"N_x = \sum_{y=x}^{\infty} v^y l_y",
        s,
        flags=re.IGNORECASE,
    )
    return s


def _normalize_insurance_actuarial_sum_notation(text: str) -> str:
    """Normalize simple actuarial sum formulas such as IA_x."""
    s = text
    # If the OCR result contains the characteristic fragments of the IA_x formula,
    # prefer a canonical actuarial expression over hallucinated prose.
    compact = re.sub(r"\s+", "", s)
    ia_pattern = r"I\s*A\s*(?:_\s*\{\s*x\s*\}|_?\s*x)"
    has_ia = re.search(ia_pattern, s, flags=re.IGNORECASE) or re.search(r"\b[TI]A\s*[,._]?\s*=", s, flags=re.IGNORECASE)
    has_sum_like = (
        "sum" in compact.lower()
        or "Σ" in s
        or "∑" in s
        or "k=0" in compact
        or "k=0" in s
        or re.search(r"\bS[o0]\s*\(", s, flags=re.IGNORECASE)
    )
    has_survival_terms = (
        re.search(r"p\s*_?\s*\{?\s*x\s*\}?", s, flags=re.IGNORECASE)
        or re.search(r"q\s*_?\s*\{?\s*x", s, flags=re.IGNORECASE)
        or "ype" in compact.lower()
        or "desk" in compact.lower()
    )
    if has_ia and ("q" in compact or "x" in compact or "k" in compact):
        if has_sum_like:
            return r"IA_x = \sum_{k=0}^{\infty} (k+1)v^{k+1}\,{}_kp_x \cdot q_{x+k}"
    if has_sum_like and has_survival_terms and re.search(r"v\s*(?:\^|\*|\{)?\s*k", s, flags=re.IGNORECASE):
            return r"IA_x = \sum_{k=0}^{\infty} (k+1)v^{k+1}\,{}_kp_x \cdot q_{x+k}"

    s = re.sub(r"\bI\s*A\s*x\b", r"IA_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bIA\s*x\b", r"IA_x", s, flags=re.IGNORECASE)
    s = re.sub(
        r"k\s*=\s*0\s*\(?\s*k\s*\+\s*1\s*\)?\s*v\s*k\s*\+\s*1",
        lambda _: r"\sum_{k=0}^{\infty} (k+1)v^{k+1}",
        s,
    )
    s = re.sub(r"\b1\s*k\s*p\s*x\b", r"{}_kp_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bk\s*p\s*x\b", r"{}_kp_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bq\s*x\s*\+\s*k\b", r"q_{x+k}", s, flags=re.IGNORECASE)
    s = re.sub(r"\bq\s*x\b", r"q_x", s, flags=re.IGNORECASE)
    s = re.sub(r"Thisnet\w*|Thisnetsingle\w*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"single\w*|prem\w*", "", s, flags=re.IGNORECASE)
    return s


def _normalize_actuarial_annuity_notation(text: str) -> str:
    """Normalize common actuarial annuity OCR fragments.

    This targets Korean insurance actuarial formulas where OCR often breaks
    symbols like ä_x, l_{x+j}, N_x, D_x, and nE_x into plain text fragments.
    """
    s = text
    s = s.replace("∵", "\\because")
    s = re.sub(r"\balpha\b", "α", s, flags=re.IGNORECASE)

    # Life table symbols: lx +j/lx -> l_{x+j}/l_x, Nx +n -> N_{x+n}
    for sym in ["l", "N", "D", "S"]:
        s = re.sub(rf"\b{sym}\s*x\s*\+\s*([jn0-9]+)\s*\+\s*([0-9]+)\b", rf"{sym}_{{x+\1+\2}}", s, flags=re.IGNORECASE)
        s = re.sub(rf"\b{sym}\s*x\s*\+\s*([jn0-9]+)\b", rf"{sym}_{{x+\1}}", s, flags=re.IGNORECASE)
        s = re.sub(rf"\b{sym}\s*x\b", rf"{sym}_x", s, flags=re.IGNORECASE)
    s = re.sub(r"\bl_\{x\+0\}", "l_x", s)
    s = re.sub(r"\bN_\{x\+0\}", "N_x", s)
    s = re.sub(r"\bD_\{x\+0\}", "D_x", s)

    # äx, äx+n, äx :n, n äx -> annuity-due notation.
    s = re.sub(r"ä\s*x\s*\+\s*([n0-9]+)", r"\\ddot{a}_{x+\1}", s)
    s = re.sub(r"ä\s*x\s*:\s*n", r"\\ddot{a}_{x:\\overline{n}|}", s)
    s = re.sub(r"ä\s*x", r"\\ddot{a}_x", s)
    s = re.sub(r"\bn\s*\\ddot\{a\}_x", r"{}_n|\\ddot{a}_x", s)
    s = re.sub(r"S_x\s*:\s*n\s*\+\s*1", r"S_{x:\\overline{n+1}|}", s)
    s = re.sub(r"S_x\s*:\s*n", r"S_{x:\\overline{n}|}", s)

    # Superscript alpha written on the next line: a_x (α) -> a_x^{(α)}
    s = re.sub(r"(\\ddot\{a\}_\{?[^}\s]+\}?|\\ddot\{a\}_x)\s*\(\s*α\s*\)", r"\1^{(α)}", s)
    s = re.sub(r"(S_\{?x[^}\s]*\}?|S_x)\s*\(\s*α\s*\)", r"\1^{(α)}", s)
    s = re.sub(r"\bn\s*E\s*x\s*\(\s*α\s*\)", r"{}_nE_x^{(α)}", s)
    s = re.sub(r"\bn\s*E\s*x\b", r"{}_nE_x", s)

    # Broken summation fragments.
    s = re.sub(r"j\s*=\s*0\s+e\s*-\s*α\s*j", r"\\sum_{j=0}^{\\infty} e^{-α j}", s)
    s = re.sub(r"j\s*=\s*n\s+e\s*-\s*α\s*j", r"\\sum_{j=n}^{\\infty} e^{-α j}", s)
    s = re.sub(r"n\s*-\s*1\s+j\s*=\s*0\s+e\s*-\s*α\s*j", r"\\sum_{j=0}^{n-1} e^{-α j}", s)
    s = re.sub(r"j\s*=\s*0\s+e\s*α\s*\(\s*n\s*-\s*j\s*\)", r"\\sum_{j=0}^{n-1} e^{α(n-j)}", s)
    s = re.sub(r"n\s*-\s*1\s+\\sum_\{j=0\}\^\{\\infty\}", r"\\sum_{j=0}^{n-1}", s)

    # Discount factors.
    s = re.sub(r"e\s*-\s*α\s*\(\s*n\s*-\s*1\s*\)", r"e^{-α(n-1)}", s)
    s = re.sub(r"e\s*-\s*α\s*n", r"e^{-α n}", s)
    s = re.sub(r"e\s*-\s*α\s*j", r"e^{-α j}", s)
    s = re.sub(r"e\s*α\b", r"e^{α}", s)

    # Ratios and select symbols.
    s = re.sub(r"\(\s*l_\{x\+([jn0-9]+)\}\s*/\s*l_x\s*\)", r"(l_{x+\1}/l_x)", s)
    s = re.sub(r"\(\s*l_x\s*/\s*l_\{x\+1\}\s*\)", r"(l_x/l_{x+1})", s)
    s = re.sub(r"\(\s*N_x\s*-\s*N_\{x\+n\}\s*\)\s*/\s*D_x", r"(N_x - N_{x+n})/D_x", s)
    s = re.sub(r"N_\{x\+n\}\s*/\s*D_x", r"N_{x+n}/D_x", s)
    s = re.sub(r"N_x\s*/\s*D_x", r"N_x/D_x", s)

    return s


def _cleanup_actuarial_latex_spacing(text: str) -> str:
    s = text
    s = s.replace("^{^{(α)}}", "^{(α)}")
    s = s.replace("^{^{(α)}", "^{(α)}")
    s = s.replace("^{^{(α", "^{(α)}")
    s = re.sub(r"\^\{\s*\^\{\s*\(([^)]*)\)\s*\}\s*\}", r"^{(\1)}", s)
    s = re.sub(r"\^\{\s*\^\{\s*\(([^)]*)\)\s*$", r"^{(\1)}", s)
    s = re.sub(r"\^\{\s*\^\{\s*\(α\)\s*\}\s*\}", r"^{(α)}", s)
    s = re.sub(r"\^\{\s*\^\{\s*\(α\)\s*$", r"^{(α)}", s)
    s = re.sub(r"\^\{\s*\^\{\s*\(α\s*$", r"^{(α)}", s)
    s = re.sub(r"\^\{\s*\^\{\s*\(α\}", r"^{(α)}", s)
    s = re.sub(r"(?<!\{)\(\s*α\s*\)", r"^{(α)}", s)
    s = re.sub(r"\^\{\(α\s*$", r"^{(α)}", s)
    s = re.sub(r"\}\s+\^\{\(α\)\}", r"}^{(α)}", s)
    s = s.replace("\\overline{n + 1}", "\\overline{n+1}")
    s = re.sub(r"_\{x\s*([+-])\s*([jn0-9]+)\}", r"_{x\1\2}", s)
    s = re.sub(r"_\{x\s*([+-])\s*([jn0-9]+)\s*([+-])\s*([0-9]+)\}", r"_{x\1\2\3\4}", s)
    s = re.sub(r"l_\{x\+n\}\s*\+\s*1", r"l_{x+n+1}", s)
    s = re.sub(r"N_\{x\+n\}", r"N_{x+n}", s)
    s = re.sub(r"D_\{x\+n\}", r"D_{x+n}", s)
    s = re.sub(r"\\sum_\{j\s*=\s*0\}", r"\\sum_{j=0}", s)
    s = re.sub(r"\\sum_\{j\s*=\s*n\}", r"\\sum_{j=n}", s)
    s = re.sub(r"\\sum_\{k\s*=\s*0\}", r"\\sum_{k=0}", s)
    s = re.sub(r"\(\s*k\s*\+\s*1\s*\)", r"(k+1)", s)
    s = re.sub(r"v\^\{k\s*\+\s*1\}", r"v^{k+1}", s)
    s = re.sub(r"v\^\{k\s*\+\s*x\}", r"v^{k+x}", s)
    s = re.sub(r"q_\{x\s*\+\s*k\}", r"q_{x+k}", s)
    s = re.sub(r"l_\{k\s*\+\s*x\}", r"l_{k+x}", s)
    s = re.sub(r"\\sum_\{y\s*=\s*x\}", r"\\sum_{y=x}", s)
    return s


def _normalize_actuarial_expectation_symbols(text: str) -> str:
    """Normalize OCR variants such as 'E A EB(t,x)' into 'E_{AEB}(t,x)'."""
    def compact_symbol(match: re.Match[str]) -> str:
        symbol = re.sub(r"\s+", "", match.group(1))
        return f"E_{{{symbol}}}("

    def compact_braced_symbol(match: re.Match[str]) -> str:
        symbol = re.sub(r"\s+", "", match.group(1))
        return f"E_{{{symbol}}}("

    # E APVFNC(t,x), E A EB(t,x), E A E B (t,x)
    text = re.sub(r"\bE\s+([A-Z](?:\s*[A-Z0-9]){1,})\s*\(", compact_symbol, text)
    # E_{A EB}(t,x)
    text = re.sub(r"\bE_\{([A-Z](?:\s*[A-Z0-9]){1,})\}\s*\(", compact_braced_symbol, text)
    return text


def _trim_dangling_formula_tail(text: str) -> str:
    """Remove OCR leftovers that make the displayed formula visibly incomplete."""
    s = text.strip()
    s = re.sub(r"\s+", " ", s)
    # Drop trailing operators. Preserve balanced LaTeX braces such as q_{x+k}.
    s = re.sub(r"\s*(?:=|\+|-|×|\*|/|\\times)\s*$", "", s).strip()
    # Drop repeated closing junk only when the corresponding opener is missing.
    if s.count("{") < s.count("}") or s.count("(") < s.count(")") or s.count("[") < s.count("]"):
        s = re.sub(r"(?:\s*[\]\)}])+\s*(?:[\(\[\{]\s*)?$", "", s).strip()
    # Remove a final orphan opening bracket.
    s = re.sub(r"\s*[\(\[\{]\s*$", "", s).strip()
    return s


def normalize_latex(latex: str) -> str:
    s = latex.strip()
    replacements = {
        "\\,": "",
        "\\times": "*",
        "×": "*",
        "명": "",
        "원": "",
        ",": "",
        "\\text": "text",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s)


def extract_variables(latex: str) -> list[str]:
    found = []
    for symbol, alias in SYMBOL_ALIASES.items():
        if symbol in latex or alias in latex:
            found.append(alias)
    # E_{ANC}, E_{APVFNC} 같은 계리식 함수 후보
    for fn in re.findall(r"E_\{?([A-Z][A-Z0-9_]+)\}?", latex):
        alias = f"E_{fn}"
        if alias not in found:
            found.append(alias)
    # 영문 변수 후보
    for v in re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", latex):
        if v not in ["text", "frac", "ln", "log", "exp", "int"] and v not in found:
            found.append(v)
    if "δ" in latex and "delta" not in found:
        found.append("delta")
    return found


def build_formula_dsl(latex: str) -> dict[str, Any]:
    """운영용 완성 파서가 아니라 PoC용 DSL 후보 생성기입니다."""
    variables = extract_variables(latex)
    return {
        "type": "actuarial_formula_candidate",
        "source_latex": latex,
        "variables": variables,
        "parser_version": "local-poc-0.3-multiline-regroup",
        "note": "운영 반영 전 Rule Validator와 Golden Test를 통과해야 합니다.",
    }


def validate_formula_candidate(latex: str) -> tuple[str, str]:
    if not latex or len(latex.strip()) < 3:
        return "FAIL", "수식 문자열이 비어 있습니다."
    if latex.count("{") != latex.count("}"):
        return "FAIL", "LaTeX 중괄호 개수가 맞지 않습니다."
    if "=" not in latex and "\\frac" not in latex and "/" not in latex and "\\int" not in latex:
        return "REVIEW", "수식 연산자 또는 등호가 명확하지 않습니다."
    return "PASS", "기본 문법 검증 통과"


def try_evaluate_simple_numbers(latex: str) -> dict[str, Any] | None:
    """샘플 보험료 예제처럼 숫자 사칙연산이 명확한 경우 Decimal로 계산 후보를 생성합니다."""
    normalized = normalize_latex(latex)
    nums = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", normalized)
    if len(nums) < 2:
        return None
    try:
        values = [Decimal(n) for n in nums]
    except InvalidOperation:
        return None
    return {
        "numbers": [str(v) for v in values],
        "message": "숫자 후보를 추출했습니다. 실제 연산식 확정은 Rule Parser 단계에서 수행합니다.",
    }


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)

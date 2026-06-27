from app.formula_structure import (
    WHOLE_LIFE_CANONICAL,
    formula_diagnostics,
    repair_formula_latex,
)


def test_whole_life_canonical_detection_from_broken_ocr_text():
    raw = """
    A x : infinity = sum k=0 infinity v k+1 k p x q x+k
    = sum k=0 infinity v x+k+1 (l x+k - l x+k+1) / v x l x
    """
    repaired = repair_formula_latex(raw)
    assert repaired == WHOLE_LIFE_CANONICAL


def test_whole_life_terms_are_structured():
    raw = r"A_{x:\infty|} = \sum k=0 \infty v^{k+1} _k p_x q x+k"
    repaired = repair_formula_latex(raw)
    assert r"A_{x:\overline{\infty}|}" in repaired
    assert r"\sum_{k=0}^{\infty}" in repaired
    assert r"{}_{k}p_x" in repaired
    assert r"q_{x+k}" in repaired


def test_diagnostics_detects_actuarial_structures():
    repaired = repair_formula_latex(WHOLE_LIFE_CANONICAL)
    diag = formula_diagnostics(repaired)
    assert diag["has_sum"] is True
    assert diag["has_fraction"] is True
    assert diag["has_actuarial_overline"] is True
    assert diag["has_deferred_survival"] is True

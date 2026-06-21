import os
import re
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .db import fetch_all, fetch_one, get_conn, init_db, utc_now
from .ppt_processor import save_upload, process_document, _insert_formula

app = FastAPI(title="Actuarial Formula Page OCR Local PoC", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "OK", "service": "actuarial-formula-page-ocr-local"}


@app.post("/documents/upload")
def upload_document(file: UploadFile = File(...), ocr_engine: str = Form("formula")):
    try:
        doc_id, path = save_upload(file.file, file.filename or "upload.bin")
        result = process_document(doc_id, path, file.filename or "upload.bin", ocr_engine)
        result["ocr_engine"] = ocr_engine
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/documents")
def list_documents():
    return fetch_all(
        "SELECT id, filename, file_type, page_count, status, created_at, updated_at FROM documents ORDER BY created_at DESC"
    )


@app.get("/admin/db/summary")
def db_summary():
    tables = fetch_all(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )
    result = []
    with get_conn() as conn:
        for table in tables:
            name = table["name"]
            count = conn.execute(f'SELECT COUNT(*) AS cnt FROM "{name}"').fetchone()["cnt"]
            result.append({"table": name, "rows": count})
    return result


@app.get("/admin/db/tables/{table_name}")
def db_table(table_name: str, limit: int = 100):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise HTTPException(status_code=400, detail="invalid table name")
    exists = fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not exists:
        raise HTTPException(status_code=404, detail="table not found")
    limit = max(1, min(limit, 500))
    return fetch_all(f'SELECT * FROM "{table_name}" LIMIT ?', (limit,))


@app.post("/admin/db/query")
def db_query(payload: dict):
    sql = str(payload.get("sql") or "").strip()
    if not _is_readonly_sql(sql):
        raise HTTPException(status_code=400, detail="SELECT/WITH/PRAGMA table_info only allowed")
    try:
        return fetch_all(sql)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/admin/ocr-data/delete")
def delete_ocr_data():
    data_root = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    upload_dir = data_root / "uploads"
    asset_dir = data_root / "assets"
    with get_conn() as conn:
        conn.execute("DELETE FROM formula_validation_results")
        conn.execute("DELETE FROM formula_blocks")
        conn.execute("DELETE FROM page_assets")
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM documents")
    for target in [upload_dir, asset_dir]:
        if target.exists() and str(target).startswith(str(data_root)):
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    return {"status": "DELETED", "deleted": ["documents", "pages", "page_assets", "formula_blocks", "formula_validation_results", "uploads", "assets"]}


def _is_readonly_sql(sql: str) -> bool:
    if not sql or ";" in sql:
        return False
    lowered = re.sub(r"\s+", " ", sql).strip().lower()
    if re.search(r"\b(insert|update|delete|drop|alter|create|replace|vacuum|attach|detach|pragma\s+(?!table_info\b))\b", lowered):
        return False
    return lowered.startswith("select ") or lowered.startswith("with ") or lowered.startswith("pragma table_info")


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    doc = fetch_one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    pages = fetch_all(
        "SELECT page_no, title, substr(extracted_text,1,300) AS preview, image_path FROM pages WHERE document_id=? ORDER BY page_no",
        (document_id,),
    )
    formulas = fetch_all(
        "SELECT id,page_no,formula_seq,formula_title,latex,confidence,source_type,status,validation_message FROM formula_blocks WHERE document_id=? ORDER BY page_no, formula_seq",
        (document_id,),
    )
    return {"document": doc, "pages": pages, "formulas": formulas}


@app.get("/documents/{document_id}/pages")
def list_pages(document_id: str):
    return fetch_all(
        "SELECT page_no, title, substr(extracted_text,1,500) AS preview, image_path, ocr_status FROM pages WHERE document_id=? ORDER BY page_no",
        (document_id,),
    )


@app.get("/documents/{document_id}/pages/{page_no}")
def get_page(document_id: str, page_no: int):
    page = fetch_one("SELECT * FROM pages WHERE document_id=? AND page_no=?", (document_id, page_no))
    if not page:
        raise HTTPException(status_code=404, detail="page not found")
    assets = fetch_all(
        "SELECT id,asset_type,asset_path,raw_text,metadata_json,created_at FROM page_assets WHERE document_id=? AND page_no=? ORDER BY id",
        (document_id, page_no),
    )
    formulas = fetch_all(
        "SELECT * FROM formula_blocks WHERE document_id=? AND page_no=? ORDER BY formula_seq",
        (document_id, page_no),
    )
    return {"page": page, "assets": assets, "formulas": formulas}


@app.get("/formulas/search")
def search_formulas(q: str = ""):
    like = f"%{q}%"
    return fetch_all(
        """
        SELECT f.id, f.document_id, d.filename, f.page_no, f.formula_seq, f.formula_title,
               f.latex, f.confidence, f.source_type, f.status, f.validation_message
        FROM formula_blocks f
        JOIN documents d ON d.id = f.document_id
        WHERE f.latex LIKE ? OR f.raw_text LIKE ? OR f.formula_title LIKE ? OR d.filename LIKE ?
        ORDER BY d.created_at DESC, f.page_no, f.formula_seq
        LIMIT 100
        """,
        (like, like, like, like),
    )


@app.post("/formulas/{formula_id}/approve")
def approve_formula(formula_id: int):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE formula_blocks SET status='APPROVED', updated_at=? WHERE id=?",
            (utc_now(), formula_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="formula not found")
    return {"formula_id": formula_id, "status": "APPROVED"}


@app.post("/formulas/{formula_id}/reject")
def reject_formula(formula_id: int):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE formula_blocks SET status='REJECTED', updated_at=? WHERE id=?",
            (utc_now(), formula_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="formula not found")
    return {"formula_id": formula_id, "status": "REJECTED"}


@app.post("/formulas/{formula_id}/revalidate")
def revalidate_formula(formula_id: int):
    from .formula_parser import (
        build_formula_dsl,
        normalize_broken_formula_text,
        to_json,
        try_evaluate_simple_numbers,
        validate_formula_candidate,
    )

    row = fetch_one("SELECT * FROM formula_blocks WHERE id=?", (formula_id,))
    if not row:
        raise HTTPException(status_code=404, detail="formula not found")

    source = row.get("raw_text") or row.get("latex") or ""
    normalized = normalize_broken_formula_text(source)
    dsl = build_formula_dsl(normalized)
    numeric_hint = try_evaluate_simple_numbers(normalized)
    if numeric_hint:
        dsl["numeric_hint"] = numeric_hint
    status, msg = validate_formula_candidate(normalized)
    final_status = "CANDIDATE" if status == "PASS" else "NEEDS_REVIEW"

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE formula_blocks
               SET latex=?, normalized_latex=?, formula_dsl_json=?, variables_json=?,
                   status=?, validation_message=?, updated_at=?
             WHERE id=?
            """,
            (
                normalized,
                normalized,
                to_json(dsl),
                to_json(dsl.get("variables", [])),
                final_status,
                msg,
                utc_now(),
                formula_id,
            ),
        )
        conn.execute(
            "INSERT INTO formula_validation_results(formula_id,rule_name,result,message,created_at) VALUES(?,?,?,?,?)",
            (formula_id, "manual_revalidate", status, msg, utc_now()),
        )

    return {
        "formula_id": formula_id,
        "before": row.get("latex"),
        "raw_text": source,
        "after": normalized,
        "status": final_status,
        "validation_message": msg,
        "dsl": dsl,
    }


@app.post("/documents/{document_id}/revalidate-formulas")
def revalidate_document_formulas(document_id: str):
    doc = fetch_one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    rows = fetch_all("SELECT id FROM formula_blocks WHERE document_id=? ORDER BY page_no, formula_seq", (document_id,))
    updated = []
    for row in rows:
        updated.append(revalidate_formula(int(row["id"])))
    return {"document_id": document_id, "revalidated_formulas": len(updated), "items": updated[:20]}


@app.post("/documents/{document_id}/regroup-formulas")
def regroup_document_formulas(document_id: str):
    """기존 OCR 결과가 줄 단위로 잘렸을 때, 저장된 페이지/자산 텍스트를 다시 묶어 formula_blocks를 재생성합니다."""
    from .formula_parser import extract_formula_candidates

    doc = fetch_one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")

    inserted = 0
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM formula_validation_results WHERE formula_id IN (SELECT id FROM formula_blocks WHERE document_id=?)",
            (document_id,),
        )
        conn.execute("DELETE FROM formula_blocks WHERE document_id=?", (document_id,))

        pages = conn.execute(
            "SELECT page_no, extracted_text FROM pages WHERE document_id=? ORDER BY page_no",
            (document_id,),
        ).fetchall()

        for page in pages:
            page_no = int(page["page_no"])
            text_parts = [page["extracted_text"] or ""]
            assets = conn.execute(
                "SELECT raw_text FROM page_assets WHERE document_id=? AND page_no=? ORDER BY id",
                (document_id, page_no),
            ).fetchall()
            text_parts.extend([a["raw_text"] or "" for a in assets])
            combined = "\n".join([t for t in text_parts if t])
            seq = 1
            for cand in extract_formula_candidates(combined):
                cand["formula_seq"] = seq
                _insert_formula(conn, document_id, page_no, cand)
                inserted += 1
                seq += 1

    return {"document_id": document_id, "regrouped_formulas": inserted, "status": "DONE"}


@app.get("/assets")
def get_asset(path: str):
    # /data/assets 하위만 허용
    p = Path(path).resolve()
    data_root = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    if not str(p).startswith(str(data_root)) or not p.exists():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(str(p))

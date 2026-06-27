import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'UPLOADED',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                title TEXT,
                extracted_text TEXT,
                image_path TEXT,
                ocr_status TEXT NOT NULL DEFAULT 'DONE',
                created_at TEXT NOT NULL,
                UNIQUE(document_id, page_no),
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS page_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                asset_type TEXT NOT NULL,
                asset_path TEXT,
                raw_text TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS formula_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                formula_seq INTEGER NOT NULL,
                formula_title TEXT,
                raw_text TEXT,
                latex TEXT,
                normalized_latex TEXT,
                formula_dsl_json TEXT,
                variables_json TEXT,
                confidence REAL DEFAULT 0.0,
                source_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'CANDIDATE',
                bbox_json TEXT,
                validation_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS formula_validation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                formula_id INTEGER NOT NULL,
                rule_name TEXT NOT NULL,
                result TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(formula_id) REFERENCES formula_blocks(id) ON DELETE CASCADE
            );

            -- Ordered mixed-content view of a page.
            -- This table is for display/search/RAG/review. formula_blocks remains
            -- the canonical place for validated formulas.
            CREATE TABLE IF NOT EXISTS page_content_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                block_seq INTEGER NOT NULL,
                block_type TEXT NOT NULL,       -- text, formula, table, image, note
                role TEXT,                      -- paragraph, equation, caption, heading
                text_content TEXT,              -- human text or display text
                latex TEXT,                     -- canonical LaTeX for formula block
                formula_id INTEGER,
                bbox_json TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                FOREIGN KEY(formula_id) REFERENCES formula_blocks(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(document_id, page_no);
            CREATE INDEX IF NOT EXISTS idx_formula_doc_page ON formula_blocks(document_id, page_no);
            CREATE INDEX IF NOT EXISTS idx_formula_status ON formula_blocks(status);
            CREATE INDEX IF NOT EXISTS idx_content_doc_page ON page_content_blocks(document_id, page_no, block_seq);
            CREATE INDEX IF NOT EXISTS idx_content_type ON page_content_blocks(block_type);
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ["formula_dsl_json", "variables_json", "bbox_json", "metadata_json"]:
        if key in d and d[key]:
            try:
                d[key.replace("_json", "")] = json.loads(d[key])
            except Exception:
                d[key.replace("_json", "")] = d[key]
    return d


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [row_to_dict(r) for r in rows]


def fetch_one(query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
        return row_to_dict(row)

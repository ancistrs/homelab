#!/usr/bin/env python3
"""
kb-sync: Knowledge Base embedding sync using OpenAI text-embedding-3-large.

Sources
-------
  paperless    – Paperless-ngx archive PDFs (OCR'd by Paperless)
  obsidian     – Obsidian vault (text + PDF + image files)
  google-drive – Google Drive "Knowledge Base" folder

Sync strategy
-------------
  SHA-based CRUD per source:
    1. Collect current SHA set from the source.
    2. Delete any rows in Postgres whose SHA is no longer present (orphans).
    3. For each current document, skip if SHA already exists; else embed + insert.

Text extraction
---------------
  Text files       → read directly
  Paperless PDFs   → read from archive/ (Paperless OCR'd versions)
  Other PDFs       → extract text via PyMuPDF; if empty, OCR pages via Gemini Flash
  Images           → OCR via Gemini Flash (OpenRouter)

Chunking
--------
  All text → tiktoken chunks of 1000 tokens, 200-token overlap

SHA formula
-----------
  google-drive : SHA256("{drive_id}|||{filename}|||{content_or_bytes}")
  obsidian     : SHA256("obsidian|{rel_path}|{content_or_bytes}")
  paperless    : SHA256("paperless|{rel_path}|{pdf_bytes}")
"""

import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import psycopg2
import requests
import tiktoken
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from pgvector.psycopg2 import register_vector

# ── Load environment ──────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR.parent.parent / ".env")

# ── Configuration ─────────────────────────────────────────────────────────────

TABLE_NAME           = "kb_index"
EMBEDDING_MODEL      = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-large")
EMBEDDING_DIMS       = 3072
CHUNK_SIZE_TOKENS    = 1000
CHUNK_OVERLAP_TOKENS = 200

# Source paths
OBSIDIAN_PATH            = Path(os.getenv("OBSIDIAN_PATH",            "/Users/fink/PAOS/vault"))
PAPERLESS_ORIGINALS_PATH = Path(os.getenv("PAPERLESS_ORIGINALS_PATH", "/Users/fink/PAOS/code/homelab/paperless/media/documents/originals"))
PAPERLESS_ARCHIVE_PATH   = Path(os.getenv("PAPERLESS_ARCHIVE_PATH",   "/Users/fink/PAOS/code/homelab/paperless/media/documents/archive"))
GOOGLE_KB_FOLDER_NAME    = os.getenv("GOOGLE_KB_FOLDER_NAME",         "Knowledge Base")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    str(_SCRIPT_DIR / "google_service_account.json"),
)

# Obsidian: when set, only scan this subfolder (e.g. "_Inbox") — skips orphan cleanup
OBSIDIAN_SUBFOLDER = os.getenv("OBSIDIAN_SUBFOLDER", "")

# Postgres
PG_HOST     = os.getenv("KB_PG_HOST",             "127.0.0.1")
PG_PORT     = int(os.getenv("KB_PG_PORT",          "5432"))
PG_DB       = os.getenv("KB_PG_DB",               "ancistrs")
PG_USER     = os.getenv("KB_PG_USER",             "admin")
PG_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

# API keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Rate limiting
EMBED_DELAY_S = float(os.getenv("KB_EMBED_DELAY_S", "0.5"))

# OCR model (via OpenRouter)
_OCR_MODEL = os.getenv("OPENROUTER_OCR_MODEL", "google/gemini-3.1-flash-lite-preview")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kb-sync")

# ── SHA helpers ───────────────────────────────────────────────────────────────

def _sha256(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode())
    return h.hexdigest()

# ── OpenRouter API ────────────────────────────────────────────────────────────

def _openrouter_request(payload: dict, max_retries: int = 4) -> dict:
    """POST to OpenRouter chat/completions with retry on transient errors."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    delay = 2.0
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=90,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            err = str(e).lower()
            if attempt < max_retries - 1 and any(k in err for k in (
                "429", "500", "502", "503", "504", "timeout", "timed out", "rate"
            )):
                wait = delay * (2 ** attempt)
                log.warning("OpenRouter attempt %d/%d failed (retrying in %.0fs): %s",
                            attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"OpenRouter failed after {max_retries} retries")

# ── Embedding via OpenRouter ──────────────────────────────────────────────────

def embed_text(text: str) -> list[float]:
    """Embed text using OpenAI text-embedding-3-large via OpenRouter."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": EMBEDDING_MODEL,
        "input": text,
        "dimensions": EMBEDDING_DIMS,
    }
    delay = 2.0
    for attempt in range(4):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers=headers, json=payload, timeout=60,
            )
            resp.raise_for_status()
            time.sleep(EMBED_DELAY_S)
            return resp.json()["data"][0]["embedding"]
        except Exception as e:
            err = str(e).lower()
            if attempt < 3 and any(k in err for k in (
                "429", "500", "502", "503", "504", "timeout", "timed out", "rate"
            )):
                wait = delay * (2 ** attempt)
                log.warning("Embed attempt %d/4 failed (retrying in %.0fs): %s",
                            attempt + 1, wait, e)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Embedding failed after 4 retries")

# ── OCR via OpenRouter ────────────────────────────────────────────────────────

def ocr_image(img_bytes: bytes, mime_type: str) -> str:
    """Extract text from an image using Gemini Flash via OpenRouter. Retries on transient errors."""
    b64 = base64.b64encode(img_bytes).decode()
    payload = {
        "model": _OCR_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": """\
You are an OCR and image analysis system for a personal knowledge base. \
Your task is to extract useful, searchable content from this image.

RULES:
1. If the image contains meaningful readable text (documents, letters, articles, \
receipts, forms, handwritten notes, code, chat messages, captions, slides, \
screenshots with text), extract ALL of it verbatim. Preserve the original \
layout, line breaks, and structure as closely as possible.
2. Ignore incidental text that is not the main content: brand logos, text on \
clothing, watermarks, UI chrome, navigation elements, or single words that \
appear as part of the visual scene rather than as readable content.
3. If the image does NOT contain meaningful text (photos, drawings, diagrams, \
charts, memes, screenshots without text), write a natural description of what \
the image shows. Be specific — include people, objects, setting, actions, \
colors, and any notable details that would help someone find this image later \
through search.
4. Output ONLY the extracted text or the description. No preamble, no labels \
like 'Text:' or 'Description:', no markdown formatting, no commentary.
5. If the image is completely blank, corrupted, or unreadable, output nothing."""},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ],
        }],
    }
    try:
        result = _openrouter_request(payload)
        text = result["choices"][0]["message"]["content"]
        time.sleep(EMBED_DELAY_S)
        return (text or "").strip()
    except Exception as e:
        log.warning("OCR failed: %s", e)
        return ""


def ocr_pdf_pages(pdf_bytes: bytes) -> str:
    """Render each page of a PDF as a JPEG and OCR via Gemini Flash.
    Works for scanned PDFs of any size since pages are processed individually."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        log.warning("Cannot open PDF for OCR: %s", e)
        return ""
    all_text = []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=200)
        jpeg_bytes = pix.tobytes(output="jpeg", jpg_quality=85)
        pix = None
        page_text = ocr_image(jpeg_bytes, "image/jpeg")
        if page_text:
            all_text.append(page_text)
            log.info("OCR page %d/%d: %d chars", i + 1, len(doc), len(page_text))
    doc.close()
    return "\n\n".join(all_text)

# ── Text extraction from PDFs ────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyMuPDF. Returns empty string if no text found."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n\n".join(doc[i].get_text() for i in range(len(doc))).strip()
        doc.close()
        return text
    except Exception as e:
        log.warning("PyMuPDF text extraction failed: %s", e)
        return ""

# ── Tokenizer + text chunker ─────────────────────────────────────────────────

_enc = tiktoken.get_encoding("cl100k_base")

def chunk_text(text: str) -> list[str]:
    """Split text into 1000-token chunks with 200-token overlap."""
    tokens = _enc.encode(text)
    if not tokens:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + CHUNK_SIZE_TOKENS, len(tokens))
        chunks.append(_enc.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - CHUNK_OVERLAP_TOKENS
    return chunks

# ── PostgreSQL ────────────────────────────────────────────────────────────────

_conn: Optional[psycopg2.extensions.connection] = None

def _get_conn() -> psycopg2.extensions.connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD,
        )
    return _conn

def setup_db():
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id         BIGSERIAL    PRIMARY KEY,
                content    TEXT,
                embedding  VECTOR({EMBEDDING_DIMS}) NOT NULL,
                metadata   JSONB        NOT NULL DEFAULT '{{}}',
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_embedding_idx
            ON {TABLE_NAME} USING hnsw ((embedding::halfvec({EMBEDDING_DIMS})) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_metadata_gin
            ON {TABLE_NAME} USING GIN (metadata)
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_sha_idx
            ON {TABLE_NAME} ((metadata->>'sha'))
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_source_idx
            ON {TABLE_NAME} ((metadata->>'source'))
        """)
    conn.commit()
    register_vector(conn)
    log.info("DB ready: table=%s model=%s dims=%d", TABLE_NAME, EMBEDDING_MODEL, EMBEDDING_DIMS)

def _get_shas_for_source(source: str) -> set[str]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT metadata->>'sha' FROM {TABLE_NAME} "
            f"WHERE metadata->>'source' = %s AND metadata->>'sha' IS NOT NULL",
            (source,),
        )
        return {row[0] for row in cur.fetchall()}

def _delete_orphaned_shas(source: str, active_shas: set[str]) -> int:
    pg_shas = _get_shas_for_source(source)
    orphaned = pg_shas - active_shas
    if not orphaned:
        return 0
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {TABLE_NAME} "
            f"WHERE metadata->>'source' = %s AND metadata->>'sha' = ANY(%s)",
            (source, list(orphaned)),
        )
    conn.commit()
    log.info("[%s] Pruned %d orphaned document SHA(s)", source, len(orphaned))
    return len(orphaned)

def _sha_exists(sha: str, source: str) -> bool:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {TABLE_NAME} "
            f"WHERE metadata->>'sha' = %s AND metadata->>'source' = %s LIMIT 1",
            (sha, source),
        )
        return cur.fetchone() is not None

def _insert_chunk(content: str, embedding: list[float], metadata: dict) -> int:
    """Insert a single chunk row. Does NOT commit — caller manages the transaction."""
    if content:
        content = content.replace("\x00", "")
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE_NAME} (content, embedding, metadata) "
            f"VALUES (%s, %s, %s) RETURNING id",
            (content, embedding, json.dumps(metadata)),
        )
        return cur.fetchone()[0]

def _link_chunks(chunk_ids: list[int]):
    """Back-fill prev_chunk_id / next_chunk_id after all chunks are inserted."""
    if len(chunk_ids) < 2:
        return
    conn = _get_conn()
    with conn.cursor() as cur:
        for i, cid in enumerate(chunk_ids):
            patch = {
                "prev_chunk_id": chunk_ids[i - 1] if i > 0 else None,
                "next_chunk_id": chunk_ids[i + 1] if i < len(chunk_ids) - 1 else None,
            }
            cur.execute(
                f"UPDATE {TABLE_NAME} SET metadata = metadata || %s::jsonb WHERE id = %s",
                (json.dumps(patch), cid),
            )

def _commit():
    _get_conn().commit()

def _rollback():
    try:
        conn = _get_conn()
        if not conn.closed:
            conn.rollback()
    except Exception:
        pass

# ── Embed helpers ─────────────────────────────────────────────────────────────

def _embed_text_doc(text: str, base_meta: dict) -> list[int]:
    """Chunk text, embed each chunk, insert rows. Returns list of inserted IDs."""
    chunks = chunk_text(text)
    if not chunks:
        return []
    chunk_ids = []
    for i, chunk in enumerate(chunks):
        meta = {**base_meta, "chunk_index": i, "total_chunks": len(chunks)}
        embedding = embed_text(chunk)
        chunk_ids.append(_insert_chunk(chunk, embedding, meta))
    _link_chunks(chunk_ids)
    return chunk_ids


def _embed_pdf_doc(pdf_bytes: bytes, base_meta: dict) -> list[int]:
    """Extract text from PDF, chunk, embed. Falls back to OCR for scanned PDFs.
    Returns list of inserted IDs."""
    text = extract_pdf_text(pdf_bytes)
    if not text:
        log.info("No extractable text, trying page-by-page OCR")
        text = ocr_pdf_pages(pdf_bytes)
    if not text:
        raise RuntimeError("No text extracted and OCR returned nothing")
    base_meta["mime_type"] = "application/pdf"
    return _embed_text_doc(text, base_meta)

# ── Source: Paperless ─────────────────────────────────────────────────────────

def process_paperless() -> dict:
    stats = {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}
    source = "paperless"
    if not PAPERLESS_ORIGINALS_PATH.is_dir():
        log.error("[paperless] Path does not exist: %s", PAPERLESS_ORIGINALS_PATH)
        return stats
    log.info("[paperless] Scanning %s", PAPERLESS_ORIGINALS_PATH)

    # Pass 1: Collect SHAs from originals (canonical bytes for change detection)
    file_index: dict[str, dict] = {}
    for path in PAPERLESS_ORIGINALS_PATH.rglob("*.pdf"):
        if not path.is_file():
            continue
        try:
            pdf_bytes = path.read_bytes()
        except Exception as e:
            log.warning("[paperless] Cannot read %s: %s", path.name, e)
            continue
        rel = str(path.relative_to(PAPERLESS_ORIGINALS_PATH))
        sha = _sha256(b"paperless|" + rel.encode() + b"|" + pdf_bytes)
        file_index[sha] = {"path": path, "rel": rel}

    stats["found"] = len(file_index)
    log.info("[paperless] Found %d PDF(s)", len(file_index))
    stats["deleted"] = _delete_orphaned_shas(source, set(file_index.keys()))

    # Pass 2: Embed new/changed documents using archive (OCR'd) versions
    for sha, info in file_index.items():
        if _sha_exists(sha, source):
            stats["skipped"] += 1
            continue
        base_meta = {
            "sha":       sha,
            "source":    source,
            "filename":  info["path"].name,
            "file_path": info["rel"],
        }
        try:
            # Prefer archive (Paperless OCR'd), fall back to original
            archive_path = PAPERLESS_ARCHIVE_PATH / info["rel"]
            if archive_path.is_file():
                pdf_bytes = archive_path.read_bytes()
            else:
                pdf_bytes = info["path"].read_bytes()
            ids = _embed_pdf_doc(pdf_bytes, base_meta)
            _commit()
        except Exception as e:
            _rollback()
            log.error("[paperless] Failed to embed %s: %s", info["rel"], e)
            stats["failed"] += 1
            continue
        if ids:
            stats["new"] += 1
            log.info("[paperless] Embedded: %s (%d chunk(s))", info["rel"], len(ids))

    log.info("[paperless] Done — %d new, %d deleted, %d failed, %d unchanged",
             stats["new"], stats["deleted"], stats["failed"], stats["skipped"])
    return stats

# ── Source: Obsidian ──────────────────────────────────────────────────────────

_OBSIDIAN_TEXT_EXTS  = {
    ".md", ".markdown", ".txt", ".json", ".csv",
    ".yaml", ".yml", ".rst", ".tex", ".org", ".html", ".htm",
}
_OBSIDIAN_PDF_EXT    = ".pdf"
_OBSIDIAN_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
_IMAGE_MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff",
}
_TEXT_MIME_MAP = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".json": "application/json", ".csv": "text/csv",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".rst": "text/x-rst",
    ".tex": "text/x-tex", ".org": "text/x-org",
    ".html": "text/html", ".htm": "text/html",
}

def process_obsidian() -> dict:
    stats = {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}
    source = "obsidian"
    scan_root = OBSIDIAN_PATH / OBSIDIAN_SUBFOLDER if OBSIDIAN_SUBFOLDER else OBSIDIAN_PATH
    if not scan_root.is_dir():
        log.error("[obsidian] Path does not exist: %s", scan_root)
        return stats
    log.info("[obsidian] Scanning %s", scan_root)

    # Pass 1: Walk files, compute SHAs
    file_index: dict[str, dict] = {}
    counts = {"text": 0, "pdf": 0, "image": 0, "skipped": 0}

    for path in scan_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(OBSIDIAN_PATH).parts):
            continue
        rel = str(path.relative_to(OBSIDIAN_PATH))
        ext = path.suffix.lower()

        if ext in _OBSIDIAN_TEXT_EXTS:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                log.warning("[obsidian] Cannot read %s: %s", rel, e)
                continue
            sha = _sha256("obsidian|", rel, "|", content)
            file_index[sha] = {"type": "text", "path": path, "rel": rel, "ext": ext}
            counts["text"] += 1

        elif ext == _OBSIDIAN_PDF_EXT:
            try:
                pdf_bytes = path.read_bytes()
            except Exception as e:
                log.warning("[obsidian] Cannot read %s: %s", rel, e)
                continue
            sha = _sha256(b"obsidian|" + rel.encode() + b"|" + pdf_bytes)
            file_index[sha] = {"type": "pdf", "path": path, "rel": rel, "ext": ext}
            counts["pdf"] += 1

        elif ext in _OBSIDIAN_IMAGE_EXTS:
            try:
                img_bytes = path.read_bytes()
            except Exception as e:
                log.warning("[obsidian] Cannot read %s: %s", rel, e)
                continue
            sha = _sha256(b"obsidian|" + rel.encode() + b"|" + img_bytes)
            file_index[sha] = {"type": "image", "path": path, "rel": rel, "ext": ext,
                               "mime": _IMAGE_MIME_MAP[ext]}
            counts["image"] += 1

        else:
            counts["skipped"] += 1

    stats["found"] = counts["text"] + counts["pdf"] + counts["image"]
    log.info("[obsidian] Found %d text, %d PDF, %d image (%d skipped)",
             counts["text"], counts["pdf"], counts["image"], counts["skipped"])

    # Orphan cleanup (skip when scanning a subfolder)
    if not OBSIDIAN_SUBFOLDER:
        stats["deleted"] = _delete_orphaned_shas(source, set(file_index.keys()))
    else:
        log.info("[obsidian] Subfolder mode (%s) — skipping orphan cleanup", OBSIDIAN_SUBFOLDER)

    # Pass 2: Embed new/changed documents
    for sha, info in file_index.items():
        if _sha_exists(sha, source):
            stats["skipped"] += 1
            continue
        base_meta = {
            "sha":       sha,
            "source":    source,
            "filename":  Path(info["rel"]).name,
            "file_path": info["rel"],
        }
        try:
            if info["type"] == "text":
                content = info["path"].read_text(encoding="utf-8", errors="replace")
                base_meta["mime_type"] = _TEXT_MIME_MAP.get(info["ext"], "text/plain")
                ids = _embed_text_doc(content, base_meta)

            elif info["type"] == "pdf":
                pdf_bytes = info["path"].read_bytes()
                ids = _embed_pdf_doc(pdf_bytes, base_meta)

            else:  # image
                img_bytes = info["path"].read_bytes()
                base_meta["mime_type"] = info["mime"]
                ocr_text = ocr_image(img_bytes, info["mime"])
                if not ocr_text:
                    log.warning("[obsidian] OCR returned nothing for %s, skipping", info["rel"])
                    stats["failed"] += 1
                    continue
                meta = {**base_meta, "chunk_index": 0, "total_chunks": 1}
                embedding = embed_text(ocr_text)
                _insert_chunk(ocr_text, embedding, meta)
                ids = [1]

            _commit()
        except Exception as e:
            _rollback()
            log.error("[obsidian] Failed to embed %s: %s", info["rel"], e)
            stats["failed"] += 1
            continue
        if ids:
            stats["new"] += 1
            log.info("[obsidian] Embedded: %s (%s, %d chunk(s))", info["rel"], info["type"], len(ids))

    log.info("[obsidian] Done — %d new, %d deleted, %d failed, %d unchanged",
             stats["new"], stats["deleted"], stats["failed"], stats["skipped"])
    return stats

# ── Source: Google Drive ──────────────────────────────────────────────────────

_GDRIVE_TEXT_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "text/plain", "text/markdown", "application/json",
}
_GDRIVE_PDF_MIME = "application/pdf"

def _build_drive_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _find_kb_folder_id(service) -> str:
    resp = service.files().list(
        q=(f"name='{GOOGLE_KB_FOLDER_NAME}' "
           "and mimeType='application/vnd.google-apps.folder' "
           "and trashed=false"),
        fields="files(id, name)", pageSize=10,
    ).execute()
    files = resp.get("files", [])
    if not files:
        raise RuntimeError(f"Google Drive folder '{GOOGLE_KB_FOLDER_NAME}' not found.")
    return files[0]["id"]

def _list_drive_files(service, folder_id: str) -> list[dict]:
    all_files: list[dict] = []
    queue = [folder_id]
    while queue:
        fid = queue.pop()
        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{fid}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000, pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    queue.append(f["id"])
                else:
                    all_files.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return all_files

def _download_text(service, file_id: str, mime_type: str) -> str:
    if mime_type == "application/vnd.google-apps.document":
        data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        data = service.files().export(fileId=file_id, mimeType="text/csv").execute()
    elif mime_type == "application/vnd.google-apps.presentation":
        data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
    else:
        data = service.files().get_media(fileId=file_id).execute()
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)

def _download_bytes(service, file_id: str) -> bytes:
    return service.files().get_media(fileId=file_id).execute()

def process_google_drive() -> dict:
    stats = {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}
    source = "google-drive"
    if not Path(GOOGLE_SERVICE_ACCOUNT_JSON).is_file():
        log.error("[google-drive] Service account JSON not found: %s", GOOGLE_SERVICE_ACCOUNT_JSON)
        return stats
    log.info("[google-drive] Connecting via service account")
    try:
        service   = _build_drive_service()
        folder_id = _find_kb_folder_id(service)
        files     = _list_drive_files(service, folder_id)
    except Exception as e:
        log.error("[google-drive] Setup failed: %s", e)
        return stats

    log.info("[google-drive] %d file(s) found in '%s'", len(files), GOOGLE_KB_FOLDER_NAME)

    docs: dict[str, dict] = {}
    for f in files:
        file_id  = f["id"]
        filename = f["name"]
        mime     = f["mimeType"]

        if mime in _GDRIVE_TEXT_MIMES:
            try:
                content = _download_text(service, file_id, mime)
            except Exception as e:
                log.warning("[google-drive] Cannot download %s: %s", filename, e)
                continue
            sha = _sha256(f"{file_id}|||{filename}|||{content}")
            docs[sha] = {"type": "text", "id": file_id, "filename": filename,
                         "content": content, "mime": mime}

        elif mime == _GDRIVE_PDF_MIME:
            try:
                pdf_bytes = _download_bytes(service, file_id)
            except Exception as e:
                log.warning("[google-drive] Cannot download %s: %s", filename, e)
                continue
            sha = _sha256(f"{file_id}|||{filename}|||".encode() + pdf_bytes)
            docs[sha] = {"type": "pdf", "id": file_id, "filename": filename,
                         "pdf_bytes": pdf_bytes, "mime": mime}
        else:
            log.debug("[google-drive] Skipping unsupported MIME %s: %s", mime, filename)

    stats["found"] = len(docs)
    stats["deleted"] = _delete_orphaned_shas(source, set(docs.keys()))

    for sha, doc in docs.items():
        if _sha_exists(sha, source):
            stats["skipped"] += 1
            continue
        base_meta = {
            "sha":       sha,
            "source":    source,
            "filename":  doc["filename"],
            "drive_id":  doc["id"],
            "mime_type": doc["mime"],
        }
        try:
            if doc["type"] == "text":
                text = f"{doc['filename']}\n\n{doc['content']}"
                ids = _embed_text_doc(text, base_meta)
            else:
                ids = _embed_pdf_doc(doc["pdf_bytes"], base_meta)
            _commit()
        except Exception as e:
            _rollback()
            log.error("[google-drive] Failed to embed %s: %s", doc["filename"], e)
            stats["failed"] += 1
            continue
        if ids:
            stats["new"] += 1
            log.info("[google-drive] Embedded: %s (%s, %d chunk(s))",
                     doc["filename"], doc["type"], len(ids))

    log.info("[google-drive] Done — %d new, %d deleted, %d failed, %d unchanged",
             stats["new"], stats["deleted"], stats["failed"], stats["skipped"])
    return stats

# ── Main ──────────────────────────────────────────────────────────────────────

SOURCES = [
    ("paperless",    process_paperless),
    ("obsidian",     process_obsidian),
    ("google-drive", process_google_drive),
]

_SOURCE_NAMES = {name for name, _ in SOURCES}

def main():
    requested = [a for a in sys.argv[1:] if not a.startswith("-")]
    if requested:
        unknown = set(requested) - _SOURCE_NAMES
        if unknown:
            log.error("Unknown source(s): %s. Valid: %s", unknown, _SOURCE_NAMES)
            sys.exit(1)
        active = [(n, fn) for n, fn in SOURCES if n in requested]
    else:
        active = SOURCES

    t0 = time.time()
    log.info("kb-sync starting (table=%s, embedding=%s, ocr=%s, sources=%s)",
             TABLE_NAME, EMBEDDING_MODEL, _OCR_MODEL, [n for n, _ in active])
    setup_db()
    all_stats: dict[str, dict] = {}
    for name, fn in active:
        try:
            result = fn()
            all_stats[name] = result or {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}
        except Exception as e:
            log.error("[%s] Unhandled error: %s", name, e, exc_info=True)
            all_stats[name] = {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}

    log.info("─── Summary ───────────────────────────────────────────────")
    log.info("%-14s %6s %5s %8s %7s %10s", "Source", "Found", "New", "Deleted", "Failed", "Unchanged")
    totals = {"found": 0, "new": 0, "deleted": 0, "failed": 0, "skipped": 0}
    for name, s in all_stats.items():
        log.info("%-14s %6d %5d %8d %7d %10d", name, s["found"], s["new"], s["deleted"], s["failed"], s["skipped"])
        for k in totals:
            totals[k] += s[k]
    log.info("%-14s %6d %5d %8d %7d %10d", "TOTAL", totals["found"], totals["new"], totals["deleted"], totals["failed"], totals["skipped"])
    elapsed = time.time() - t0
    minutes, seconds = divmod(int(elapsed), 60)
    log.info("───────────────────────────────────────────────────────────")
    log.info("Completed in %dm %ds", minutes, seconds)

if __name__ == "__main__":
    main()

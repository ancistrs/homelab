#!/usr/bin/env python3
"""
kb-query: Semantic search API for the kb_index knowledge base.

Embeds queries with OpenAI text-embedding-3-large (via OpenRouter)
and searches pgvector via HNSW index. Optional Cohere reranking.

Usage:
    source ~/.venvs/kb-sync/bin/activate
    uvicorn kb_query:app --host 0.0.0.0 --port 8100 --app-dir scripts/kb-sync
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from pgvector.psycopg2 import register_vector
from pydantic import BaseModel

# ── Load environment ──────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR.parent.parent / ".env")

# ── Configuration ─────────────────────────────────────────────────────────────

TABLE_NAME      = "kb_index"
EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-large")
EMBEDDING_DIMS  = 3072

_SOURCE_ROOTS = {
    "obsidian":  Path(os.getenv("OBSIDIAN_PATH", "/Users/fink/PAOS/vault")),
    "paperless": Path(os.getenv("PAPERLESS_ORIGINALS_PATH",
                                "/Users/fink/PAOS/code/homelab/paperless/media/documents/originals")),
}

PG_HOST     = os.getenv("KB_PG_HOST",             "127.0.0.1")
PG_PORT     = int(os.getenv("KB_PG_PORT",          "5432"))
PG_DB       = os.getenv("KB_PG_DB",               "ancistrs")
PG_USER     = os.getenv("KB_PG_USER",             "admin")
PG_PASSWORD = os.getenv("POSTGRES_ADMIN_PASSWORD", "")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
COHERE_API_KEY     = os.getenv("COHERE_API_KEY", "")
COHERE_RERANK_MODEL = os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-pro")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kb-query")

# ── Embedding via OpenRouter ──────────────────────────────────────────────────

def embed_query(text: str) -> list[float]:
    """Embed a search query using OpenAI text-embedding-3-large via OpenRouter."""
    resp = requests.post(
        "https://openrouter.ai/api/v1/embeddings",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": EMBEDDING_MODEL,
            "input": text,
            "dimensions": EMBEDDING_DIMS,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]

# ── Cohere reranking ──────────────────────────────────────────────────────────

def rerank(query: str, results: list[dict], top_n: int) -> list[dict]:
    """Rerank search results using Cohere. Returns top_n best matches."""
    if not COHERE_API_KEY or not results:
        return results[:top_n]

    documents = [r.get("content") or r.get("filename", "") for r in results]

    try:
        resp = requests.post(
            "https://api.cohere.com/v2/rerank",
            headers={
                "Authorization": f"Bearer {COHERE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": COHERE_RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
            timeout=30,
        )
        resp.raise_for_status()
        ranked = resp.json()["results"]
        return [
            {**results[r["index"]], "rerank_score": round(r["relevance_score"], 4)}
            for r in ranked
        ]
    except Exception as e:
        log.warning("Cohere rerank failed, returning vector results: %s", e)
        return results[:top_n]

# ── PostgreSQL ────────────────────────────────────────────────────────────────

_conn: Optional[psycopg2.extensions.connection] = None

def _get_conn() -> psycopg2.extensions.connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD,
        )
        register_vector(_conn)
    return _conn

def search_vectors(embedding: list[float], top_k: int = 10,
                   source: Optional[str] = None) -> list[dict]:
    """Search kb_index by cosine similarity."""
    conn = _get_conn()
    vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    # Support comma-separated sources for multi-source filtering
    if source and "," in source:
        source_list = [s.strip() for s in source.split(",") if s.strip()]
        placeholders = ",".join(["%s"] * len(source_list))
        where = f"WHERE metadata->>'source' IN ({placeholders})"
        params = (vec_literal, *source_list, vec_literal, top_k)
    elif source:
        where = "WHERE metadata->>'source' = %s"
        params = (vec_literal, source, vec_literal, top_k)
    else:
        where = ""
        params = (vec_literal, vec_literal, top_k)
    sql = f"""
        SELECT id, content, metadata,
               1 - (embedding::halfvec({EMBEDDING_DIMS})
                    <=> %s::halfvec({EMBEDDING_DIMS})) AS similarity
        FROM {TABLE_NAME}
        {where}
        ORDER BY embedding::halfvec({EMBEDDING_DIMS})
                 <=> %s::halfvec({EMBEDDING_DIMS})
        LIMIT %s
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    results = []
    for row_id, content, metadata, similarity in rows:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
        source_name = meta.get("source")
        rel_path = meta.get("file_path")
        if rel_path and source_name in _SOURCE_ROOTS:
            meta["file_path"] = str(_SOURCE_ROOTS[source_name] / rel_path)
        results.append({
            "id": row_id,
            "content": content,
            "similarity": round(float(similarity), 4),
            **meta,
        })
    return results

# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="kb-query")

class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    source: Optional[str] = None
    rerank: bool = True

@app.get("/health")
def health():
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            count = cur.fetchone()[0]
        return {
            "status": "ok",
            "documents": count,
            "embedding_model": EMBEDDING_MODEL,
            "reranking": bool(COHERE_API_KEY),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/search")
def search(req: SearchRequest):
    use_rerank = req.rerank and bool(COHERE_API_KEY)
    log.info("Search: %r (top_k=%d, source=%s, rerank=%s)", req.query, req.top_k, req.source, use_rerank)
    embedding = embed_query(req.query)
    fetch_k = req.top_k * 3 if use_rerank else req.top_k
    results = search_vectors(embedding, top_k=fetch_k, source=req.source)
    if use_rerank:
        results = rerank(req.query, results, top_n=req.top_k)
    else:
        results = results[:req.top_k]
    log.info("Returning %d result(s)", len(results))
    return {"results": results, "query": req.query, "count": len(results)}

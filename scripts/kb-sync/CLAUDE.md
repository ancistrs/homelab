# kb-sync

Knowledge base embedding sync and semantic search API for the homelab. Embeds documents from multiple sources into pgvector for RAG retrieval.

## Components

- **kb_sync.py** — Nightly sync script. Reads documents from all sources, extracts text, embeds via OpenAI, stores in Postgres.
- **kb_query.py** — FastAPI search API (port 8100). Embeds queries, runs pgvector similarity search, reranks with Cohere.
- **setup_db.sql** — Reference schema (actual setup is in `kb_sync.py:setup_db()`).

## Commands

```bash
# Activate venv (required for all commands)
source ~/.venvs/kb-sync/bin/activate

# Full sync (all sources)
python scripts/kb-sync/kb_sync.py

# Single source
python scripts/kb-sync/kb_sync.py paperless
python scripts/kb-sync/kb_sync.py obsidian

# Obsidian subfolder (test mode, skips orphan cleanup)
OBSIDIAN_SUBFOLDER=_Inbox python scripts/kb-sync/kb_sync.py obsidian

# Start query server
uvicorn kb_query:app --host 0.0.0.0 --port 8100 --app-dir scripts/kb-sync

# Query the API
curl -X POST http://127.0.0.1:8100/search -H "Content-Type: application/json" \
  -d '{"query": "Mietvertrag", "top_k": 5}'

# Check health
curl http://127.0.0.1:8100/health
```

## Architecture

### Embedding Pipeline

1. **Sources**: Paperless (PDFs), Obsidian (text/PDF/images), Google Drive (Docs/Sheets/Slides/PDFs)
2. **Text extraction**: PyMuPDF for PDFs. Paperless archive (OCR'd) preferred over originals. Gemini Flash via OpenRouter for image OCR and scanned PDF fallback.
3. **Chunking**: 1000 tokens with 200-token overlap (tiktoken, cl100k_base)
4. **Embedding**: OpenAI text-embedding-3-large (3072 dims) via OpenRouter
5. **Storage**: pgvector in `ancistrs` database, `kb_index` table, HNSW index with halfvec cast

### Query Pipeline

1. Query text → embed with text-embedding-3-large (RETRIEVAL_QUERY is implicit for OpenAI)
2. pgvector cosine similarity → top_k × 3 candidates
3. Cohere rerank (model from `COHERE_RERANK_MODEL`, default `rerank-v4.0-pro`) → return top_k results with similarity scores and full metadata

### Change Detection

SHA256 of `source|relative_path|content_bytes`. On each run:
- New SHA → embed and insert
- Missing SHA → delete orphaned rows
- Existing SHA → skip (no API calls)

### OCR Fallback Chain (images and scanned PDFs)

1. PyMuPDF text extraction (PDFs only)
2. If no text: render pages as JPEG → Gemini Flash OCR via OpenRouter
3. OCR prompt extracts meaningful text or describes the image
4. If OCR returns nothing: skip (log warning)

OCR only runs for new/changed files (after SHA check).

### Database

- **Database**: `ancistrs`
- **Table**: `kb_index`
- **Columns**: id, content (TEXT), embedding (VECTOR(3072)), metadata (JSONB), created_at
- **Indexes**: HNSW (halfvec cosine), GIN on metadata, btree on sha and source
- **Metadata fields**: sha, source, filename, file_path, chunk_index, total_chunks, prev_chunk_id, next_chunk_id, mime_type

### Sources

| Source | Path | File types | Notes |
|---|---|---|---|
| paperless | `paperless/media/documents/originals` + `archive` | PDF | SHA from originals, text from archive (Paperless OCR). Falls back to originals + own OCR. |
| obsidian | `/Users/fink/PAOS/vault` | md, txt, json, csv, yaml, pdf, images | `OBSIDIAN_SUBFOLDER` env var for test mode |
| google-drive | Google Drive API | Docs, Sheets, Slides, PDFs | Requires service account at `scripts/kb-sync/google_service_account.json` |

## Environment Variables

From `.env`:
- `OPENROUTER_API_KEY` — API key for embedding and OCR calls
- `OPENROUTER_EMBEDDING_MODEL` — embedding model name (default: `openai/text-embedding-3-large`)
- `OPENROUTER_OCR_MODEL` — OCR/vision model name (default: `google/gemini-3.1-flash-lite-preview`)
- `COHERE_API_KEY` — reranking in query endpoint
- `COHERE_RERANK_MODEL` — rerank model name (default: `rerank-v4.0-pro`)
- `POSTGRES_ADMIN_PASSWORD` — database access

Optional:
- `OBSIDIAN_SUBFOLDER` — restrict obsidian scan to a subfolder (test mode)
- `KB_EMBED_DELAY_S` — delay between API calls (default 0.5s)
- `KB_PG_HOST` — Postgres host (default `127.0.0.1`). Currently set to `postgres.taildc3234.ts.net` in both LaunchAgent plists as a workaround for a broken OrbStack loopback forward (see root `CLAUDE.md`, postgres section)

## Dependencies

Python venv at `~/.venvs/kb-sync/`. Key packages:
- `google-api-python-client` + `google-auth` — Google Drive API
- `psycopg2-binary` + `pgvector` — Postgres + vector support
- `pymupdf` — PDF text extraction and page rendering
- `tiktoken` — token counting for chunking
- `fastapi` + `uvicorn` — query server
- `requests` — raw HTTP for OpenRouter (embed/OCR) and Cohere (rerank); no SDKs used
- `python-dotenv` — loads `.env` from repo root

## LaunchAgents

| Label | Schedule | Purpose |
|---|---|---|
| `homelab.kb-sync` | Daily 02:17 | Full sync all sources |
| `homelab.kb-query-server` | Always-on (KeepAlive) | Search API on port 8100 |

## Important Notes

- VACUUM FULL on `kb_index` requires killing the query server first (holds open connection, blocks exclusive lock)
- The `file_bytes` column exists but is intentionally not populated (was removed to save storage — files are accessed via `file_path`)
- Google Drive requires a service account with domain-wide delegation; the `Knowledge Base` folder must be shared with the service account email
- Paperless archive directory may have fewer files than originals (newly added docs not yet OCR'd by Paperless)

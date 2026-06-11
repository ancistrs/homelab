-- kb-sync: Database setup for kb_index
-- Run once against the ancistrs database (admin user).
-- The Python script also calls this automatically on startup via IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_index (
    id         BIGSERIAL PRIMARY KEY,
    content    TEXT,
    embedding  VECTOR(3072)    NOT NULL,
    metadata   JSONB           NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- HNSW cosine similarity index
-- HNSW via halfvec cast: pgvector caps HNSW at 2000 dims for vector type,
-- but halfvec(3072) fits within limits. Full float32 in storage, float16 in index.
-- Queries MUST cast: ORDER BY embedding::halfvec(3072) <=> $1::halfvec(3072)
CREATE INDEX IF NOT EXISTS kb_index_embedding_idx
    ON kb_index USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Fast JSONB filter lookups
CREATE INDEX IF NOT EXISTS kb_index_metadata_gin
    ON kb_index USING GIN (metadata);

-- Point lookups by SHA (existence check per document)
CREATE INDEX IF NOT EXISTS kb_index_sha_idx
    ON kb_index ((metadata->>'sha'));

-- Filter by source (obsidian / apple-notes / paperless / google-drive)
CREATE INDEX IF NOT EXISTS kb_index_source_idx
    ON kb_index ((metadata->>'source'));

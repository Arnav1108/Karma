-- data/catalog/software_specs_cache.sql
-- Cache table for agents/software_specs.py's LLM-backed minimum-spec lookup.
-- Keyed by lowercased software title; on a cache miss the caller queries the
-- LLM once and persists the result here (see software_specs.get_software_requirements).

BEGIN;

CREATE TABLE IF NOT EXISTS software_specs_cache (
    name         TEXT        PRIMARY KEY,
    category     TEXT        NOT NULL,
    gpu_tier     INTEGER     NOT NULL,
    cpu_tier     INTEGER     NOT NULL,
    vram_gb      INTEGER     NOT NULL,
    ram_gb       INTEGER     NOT NULL,
    storage_gb   INTEGER     NOT NULL,
    source       TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

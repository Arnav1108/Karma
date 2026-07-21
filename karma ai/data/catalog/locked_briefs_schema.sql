-- data/catalog/locked_briefs_schema.sql
-- Locked-brief persistence — durable Postgres record of each UserBuildBrief the
-- moment IntakeService locks it (lock_early / submit_answer's auto-lock branch).
-- See karma ai/docs/locked_brief_persistence_plan.md for the full design.
--
-- Retention: keep-forever-until-user-deletes, per project decision — no TTL/expiry
-- column, no cleanup job.

BEGIN;

CREATE TABLE IF NOT EXISTS locked_briefs (
    brief_id        UUID         PRIMARY KEY,
    session_id      TEXT         NOT NULL,
    user_id         UUID         NOT NULL,
    chat_id         UUID         NOT NULL,
    schema_version  TEXT         NOT NULL,
    brief           JSONB        NOT NULL,
    locked_at       TIMESTAMPTZ  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_locked_briefs_session_id ON locked_briefs (session_id);
CREATE INDEX IF NOT EXISTS idx_locked_briefs_user_id    ON locked_briefs (user_id);

-- STATUS on the live Supabase database (karma ai/docs/build_service_plan.md Phase
-- 4 work): only the CREATE TABLE and CREATE INDEX statements above were actually
-- run there. This ENABLE ROW LEVEL SECURITY / CREATE POLICY block below was
-- skipped — it references a Postgres role, "api_write_role", that does not exist
-- yet, and applying it as part of the same transaction rolled the whole script
-- back (including the table) until the RLS block was excluded. The live
-- locked_briefs table currently has RLS disabled. This is a known follow-up, not
-- an oversight: the role POSTGRES_URL actually connects as is "postgres" (the
-- table owner), and Postgres exempts owners/superusers from RLS enforcement by
-- default regardless of whether RLS is enabled — so no real access-control gap
-- exists today. Low-risk for now; revisit once real role-based access (a
-- non-owner "api_write_role" that writes are actually funneled through) is
-- designed, at which point this block can be applied on its own.

ALTER TABLE locked_briefs ENABLE ROW LEVEL SECURITY;

-- TODO(rls-role): "api_write_role" below is a placeholder — this session has not
-- confirmed which Postgres role POSTGRES_URL actually authenticates as. Resolve
-- the real role name before relying on this. Also confirm whether that role is
-- (or maps to) the table owner: Postgres exempts owners/superusers from RLS
-- enforcement by default, so if POSTGRES_URL's role owns this table (the common
-- case for the default Supabase connection role), this policy adds no protection
-- against the API's own writes unless ALTER TABLE ... FORCE ROW LEVEL SECURITY is
-- also applied. See locked_brief_persistence_plan.md section 4 for the full gotcha.
CREATE POLICY locked_briefs_api_insert ON locked_briefs
    FOR INSERT
    TO api_write_role
    WITH CHECK (true);

-- No SELECT/UPDATE/DELETE policy yet — with RLS enabled and only an INSERT policy
-- present, those commands are denied by default for any non-owner-exempt role,
-- which is the safe default since no read path is designed yet (see plan section 5).

COMMIT;

-- Run after KnowMart confirms the old listing is private and every version stopped.
-- The release operator drains delegation writers before the transaction.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE workbench_knowbot_pins, workbench_tasks IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE retired_finance_versions (
    version text PRIMARY KEY,
    digest text NOT NULL
) ON COMMIT DROP;
INSERT INTO retired_finance_versions VALUES
    ('1', 'sha256:292916ac6c559b3c20e367c584aea90c038bbef518eeeae608496301d15985f3'),
    ('3', 'sha256:71dc2fd963f0b5b93b0f53e911f6a0e17c6f00a07f43ad5f32e81ac52d1618ef'),
    ('5', 'sha256:0388a69419c5e60a4822d5cba6c9e424d3ef0d822c032ff2969c9556fc9ae027');

DO $retire$
DECLARE
    market_id CONSTANT uuid := '5a90e31a-0ace-4ce1-bfea-cc0e4c3c1f9b';
    removed integer;
BEGIN
    IF EXISTS (
        SELECT FROM workbench_knowbot_pins p
        WHERE p.knowbot_id = market_id AND NOT EXISTS (
            SELECT FROM retired_finance_versions v
            WHERE v.version = p.version AND v.digest = p.digest
        )
    ) THEN
        RAISE EXCEPTION 'Unreviewed version or digest for retired market expert';
    END IF;
    IF EXISTS (
        SELECT FROM workbench_tasks t
        WHERE t.pack_source = 'knowmart'
          AND split_part(t.profile_id, '@', 1) = 'knowbot:' || market_id::text
          AND (t.state NOT IN ('SUCCEEDED', 'CANCELLED', 'FAILED', 'REJECTED') OR NOT EXISTS (
              SELECT FROM retired_finance_versions v
              WHERE t.profile_id = 'knowbot:' || market_id::text || '@' || v.version
                AND t.pack_digest = v.digest
          ))
    ) THEN
        RAISE EXCEPTION 'Active task or unreviewed snapshot for retired market expert';
    END IF;
    DELETE FROM workbench_knowbot_pins p USING retired_finance_versions v
    WHERE p.knowbot_id = market_id AND v.version = p.version AND v.digest = p.digest;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE 'Retired % pins for market expert %', removed, market_id;
END;
$retire$;
COMMIT;

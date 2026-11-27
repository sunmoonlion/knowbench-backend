"""Harden workbench_capability_vector against null/scalar JSONB values and index slot domains."""

from alembic import op

revision = "20261126_0063"
down_revision = "20261124_0062"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
        CREATE OR REPLACE FUNCTION workbench_capability_vector(definition jsonb) RETURNS tsvector
        LANGUAGE sql IMMUTABLE PARALLEL SAFE SET jit = off AS $$
          WITH safe_def AS (
            SELECT CASE WHEN jsonb_typeof(definition) = 'object' THEN definition ELSE '{}'::jsonb END AS root
          ),
          declaration AS (
            SELECT CASE WHEN jsonb_typeof(root->'capability_declaration') = 'object'
              THEN root->'capability_declaration' ELSE '{}'::jsonb END AS d
            FROM safe_def
          ),
          procedure AS (
            SELECT CASE WHEN jsonb_typeof(root->'procedure') = 'object'
              THEN root->'procedure' ELSE '{}'::jsonb END AS p
            FROM safe_def
          ),
          ports AS (
            SELECT value AS slot FROM procedure,
              jsonb_each(CASE WHEN jsonb_typeof(p->'inputs') = 'object' THEN p->'inputs' ELSE '{}'::jsonb END)
            UNION ALL SELECT value FROM procedure,
              jsonb_each(CASE WHEN jsonb_typeof(p->'outputs') = 'object' THEN p->'outputs' ELSE '{}'::jsonb END)
            UNION ALL SELECT i->'slot' FROM procedure,
              jsonb_array_elements(CASE WHEN jsonb_typeof(p->'steps') = 'array' THEN p->'steps' ELSE '[]'::jsonb END) s,
              jsonb_each(CASE WHEN jsonb_typeof(s->'inputs') = 'object' THEN s->'inputs' ELSE '{}'::jsonb END) kv(k,i)
              WHERE jsonb_typeof(i) = 'object' AND i#>>'{binding,source}' IN ('caller','object_item')
            UNION ALL SELECT s->'outputs'->(e->>'output') FROM procedure,
              jsonb_each(CASE WHEN jsonb_typeof(p->'exports') = 'object' THEN p->'exports' ELSE '{}'::jsonb END) kv(k,e),
              jsonb_array_elements(CASE WHEN jsonb_typeof(p->'steps') = 'array' THEN p->'steps' ELSE '[]'::jsonb END) s
              WHERE jsonb_typeof(e) = 'object' AND jsonb_typeof(s->'outputs') = 'object' AND s->>'id'=e->>'step'
          ),
          clean_ports AS (
            SELECT slot FROM ports WHERE jsonb_typeof(slot) = 'object'
          ),
          fields AS (
            SELECT d->>'title' AS value FROM declaration
            UNION ALL SELECT d->>'purpose' FROM declaration
            UNION ALL SELECT value FROM declaration,
              jsonb_array_elements_text(CASE WHEN jsonb_typeof(d->'applies_when') = 'array' THEN d->'applies_when' ELSE '[]'::jsonb END)
            UNION ALL SELECT value FROM declaration,
              jsonb_each_text(CASE WHEN jsonb_typeof(d->'promises') = 'object' THEN d->'promises' ELSE '{}'::jsonb END)
            UNION ALL SELECT value->>'when' FROM declaration,
              jsonb_each(CASE WHEN jsonb_typeof(d->'conditions') = 'object' THEN d->'conditions' ELSE '{}'::jsonb END)
              WHERE jsonb_typeof(value) = 'object'
            UNION ALL SELECT slot->>'description' FROM clean_ports
            UNION ALL SELECT value FROM clean_ports,
              jsonb_each_text(CASE WHEN jsonb_typeof(slot->'meaning') = 'object' THEN slot->'meaning' ELSE '{}'::jsonb END)
            UNION ALL SELECT part FROM clean_ports,
              regexp_split_to_table(coalesce(slot#>>'{meaning,domain_id}', ''), '\.') AS part
              WHERE part <> ''
          ), words AS (
            SELECT m[1] AS w FROM fields,
              regexp_matches(regexp_replace(lower(coalesce(value, '')),'([㐀-鿿]+)',' \1 ','g'), '([㐀-鿿]+|[[:alnum:]]+)', 'g') m
          ), tokens AS (
            SELECT DISTINCT CASE WHEN w ~ '^[㐀-鿿]' THEN substr(w,n,2)
              ELSE left(w,64) END AS token FROM words,
              generate_series(1,CASE WHEN w ~ '^[㐀-鿿]' THEN greatest(1,length(w)-1) ELSE 1 END) n
          )
          SELECT array_to_tsvector(coalesce(array_agg(token), ARRAY[]::text[])) FROM tokens
        $$
    """)


def downgrade():
    op.execute(r"""
        CREATE OR REPLACE FUNCTION workbench_capability_vector(definition jsonb) RETURNS tsvector
        LANGUAGE sql IMMUTABLE PARALLEL SAFE SET jit = off AS $$
          WITH declaration AS (SELECT definition->'capability_declaration' AS d),
          procedure AS (SELECT definition->'procedure' AS p),
          ports AS (
            SELECT value AS slot FROM procedure, jsonb_each(p->'inputs')
            UNION ALL SELECT value FROM procedure, jsonb_each(p->'outputs')
            UNION ALL SELECT i->'slot' FROM procedure,
              jsonb_array_elements(p->'steps') s, jsonb_each(s->'inputs') kv(k,i)
              WHERE i#>>'{binding,source}' IN ('caller','object_item')
            UNION ALL SELECT s->'outputs'->(e->>'output') FROM procedure,
              jsonb_each(p->'exports') kv(k,e), jsonb_array_elements(p->'steps') s
              WHERE s->>'id'=e->>'step'
          ), fields AS (
            SELECT d->>'title' AS value FROM declaration
            UNION ALL SELECT d->>'purpose' FROM declaration
            UNION ALL SELECT value FROM declaration, jsonb_array_elements_text(d->'applies_when')
            UNION ALL SELECT value FROM declaration, jsonb_each_text(d->'promises')
            UNION ALL SELECT value->>'when' FROM declaration, jsonb_each(d->'conditions')
            UNION ALL SELECT slot->>'description' FROM ports
            UNION ALL SELECT value FROM ports, jsonb_each_text(slot->'meaning')
          ), words AS (
            SELECT m[1] AS w FROM fields,
              regexp_matches(regexp_replace(lower(value),'([㐀-鿿]+)',' \1 ','g'), '([㐀-鿿]+|[[:alnum:]]+)', 'g') m
          ), tokens AS (
            SELECT DISTINCT CASE WHEN w ~ '^[㐀-鿿]' THEN substr(w,n,2)
              ELSE left(w,64) END AS token FROM words,
              generate_series(1,CASE WHEN w ~ '^[㐀-鿿]' THEN greatest(1,length(w)-1) ELSE 1 END) n
          )
          SELECT array_to_tsvector(coalesce(array_agg(token), ARRAY[]::text[])) FROM tokens
        $$
    """)

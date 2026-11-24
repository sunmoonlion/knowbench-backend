"""Index positive capability declarations and external port descriptions at registration."""

from alembic import op

revision = "20261124_0062"
down_revision = "20261123_0061"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
        CREATE FUNCTION workbench_capability_vector(definition jsonb) RETURNS tsvector
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
    op.execute("""
        ALTER TABLE workbench_formal_methods ADD COLUMN capability_terms tsvector
        GENERATED ALWAYS AS (workbench_capability_vector(document#>'{draft,definition}')) STORED
    """)
    op.execute("""
        CREATE INDEX workbench_capability_terms ON workbench_formal_methods USING gin(capability_terms)
    """)


def downgrade():
    op.execute("ALTER TABLE workbench_formal_methods DROP COLUMN capability_terms")
    op.execute("DROP FUNCTION workbench_capability_vector(jsonb)")

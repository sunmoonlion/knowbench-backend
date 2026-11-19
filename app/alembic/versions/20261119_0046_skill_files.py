"""Split immutable Skill files from the owner's catalog, with an audited backfill."""

from alembic import op

revision = "20261119_0046"
down_revision = "20261115_0045"
branch_labels = None
depends_on = None


def upgrade():
    # Keep old writers out until both the data move and catalog constraint commit.
    op.execute("LOCK TABLE workbench_capability_assets IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""CREATE TABLE workbench_skill_files (
        asset_id uuid NOT NULL REFERENCES workbench_capability_assets(id) ON DELETE CASCADE,
        path text NOT NULL, data bytea NOT NULL, PRIMARY KEY (asset_id, path))""")
    op.execute("""CREATE FUNCTION reject_skill_file_update() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Installed Skill files are immutable'; END $$""")
    op.execute("""CREATE TRIGGER skill_files_immutable BEFORE UPDATE ON workbench_skill_files
        FOR EACH ROW EXECUTE FUNCTION reject_skill_file_update()""")
    op.execute("""INSERT INTO workbench_skill_files
        SELECT id, 'SKILL.md', convert_to(detail->>'content','UTF8')
        FROM workbench_capability_assets
        WHERE kind='skill' AND detail->>'format'='instruction-markdown'
        UNION ALL
        SELECT a.id, r.key, decode(r.value,'base64')
        FROM workbench_capability_assets a
        CROSS JOIN LATERAL jsonb_each_text(a.detail->'resources') r
        WHERE a.kind='skill' AND a.detail->>'format'='skill-package-zip'""")
    op.execute("""UPDATE workbench_capability_assets a SET detail =
        a.detail || jsonb_build_object('files', jsonb_build_array(jsonb_build_object(
        'path', f.path, 'size', octet_length(f.data), 'media_type', 'text/plain',
        'sha256', encode(sha256(f.data),'hex'))))
        FROM workbench_skill_files f WHERE a.id=f.asset_id AND f.path='SKILL.md'
        AND a.detail->>'format'='instruction-markdown'""")
    # Compare both directions, including paths, per-file raw bytes and the old
    # duplicate main document. An inconsistent old package aborts the transaction.
    op.execute("""DO $$ BEGIN
      IF EXISTS (
        SELECT FROM workbench_capability_assets a WHERE kind='skill' AND (
          (SELECT data FROM workbench_skill_files WHERE asset_id=a.id AND path='SKILL.md')
             IS DISTINCT FROM convert_to(a.detail->>'content','UTF8')
          OR NOT EXISTS (SELECT FROM workbench_skill_files WHERE asset_id=a.id AND path='SKILL.md')
          OR jsonb_array_length(a.detail->'files') IS DISTINCT FROM
             (SELECT count(*) FROM workbench_skill_files WHERE asset_id=a.id)
          OR EXISTS (SELECT FROM workbench_skill_files f WHERE f.asset_id=a.id AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(a.detail->'files') m
            WHERE m->>'path'=f.path AND (m->>'size')::integer=octet_length(f.data)
              AND m->>'sha256'=encode(sha256(f.data),'hex')))
        )
      ) THEN RAISE EXCEPTION 'Skill file reconciliation failed'; END IF;
    END $$""")
    op.execute("""UPDATE workbench_capability_assets SET detail=detail-'resources'-'content'
        WHERE kind='skill'""")
    op.create_check_constraint(
        "ck_skill_catalog_metadata",
        "workbench_capability_assets",
        "kind <> 'skill' OR NOT (detail ?| ARRAY['content','resources'])",
    )


def downgrade():
    op.execute("LOCK TABLE workbench_capability_assets IN SHARE ROW EXCLUSIVE MODE")
    op.drop_constraint("ck_skill_catalog_metadata", "workbench_capability_assets")
    op.execute("""UPDATE workbench_capability_assets a SET detail =
        (CASE WHEN a.detail->>'format'='instruction-markdown' THEN a.detail-'files'
          ELSE a.detail || jsonb_build_object('resources', (
            SELECT jsonb_object_agg(f.path, replace(encode(f.data,'base64'), chr(10),''))
            FROM workbench_skill_files f WHERE f.asset_id=a.id)) END)
        || jsonb_build_object('content', convert_from(main.data,'UTF8'))
        FROM workbench_skill_files main WHERE a.id=main.asset_id AND main.path='SKILL.md'""")
    op.drop_table("workbench_skill_files")
    op.execute("DROP FUNCTION reject_skill_file_update()")

"""Replace implicit delegation with exact-call direct grants; old authority expires."""

from alembic import op

revision = "20261115_0044"
down_revision = "20261114_0043"
branch_labels = None
depends_on = None


def upgrade():
    # Keep the exact prior documents as call evidence, never as runtime authority.
    op.execute("""UPDATE workbench_calls c SET
        detail=c.detail || jsonb_build_object('authorization_before_0044',a.document),
        updated_at=now()
        FROM workbench_method_authorizations a WHERE c.id=a.call_id""")
    op.execute("""UPDATE workbench_method_authorizations a SET
        document=a.document || jsonb_build_object(
            'revoked',true,'revision',a.revision+1,
            'grants',coalesce((SELECT jsonb_agg(g || jsonb_build_object(
                'delegable',false,'recipient_call_id',null))
                FROM jsonb_array_elements(a.document->'grants') g),'[]'::jsonb)),
        revision=a.revision+1,updated_at=now()""")
    # A turn already using the superseded authority must stop before the owner
    # can confirm again. Preserve the existing Task/Attempt recovery mechanism.
    op.execute("""UPDATE workbench_attempts attempt SET
        refs=attempt.refs || jsonb_build_object('permission_revocation',
            jsonb_build_object('call_id',a.call_id,'revision',a.revision,
                'context',s.runtime#>>'{method,context}'))
        FROM workbench_capability_selections s, workbench_method_authorizations a,
             workbench_tasks t
        WHERE s.runtime#>>'{method,call_id}'=a.call_id::text
          AND s.session_id=a.session_id AND s.owner_actor_id=a.owner_actor_id
          AND s.runtime#>>'{method,attempt_id}'=attempt.id::text
          AND attempt.task_id=t.id AND t.active_attempt_id=attempt.id
          AND t.session_id=s.session_id AND t.owner_actor_id=s.owner_actor_id
          AND attempt.status IN ('CREATED','RUNNING','SUSPENDED')
          AND s.runtime#>>'{method,context}' IS NOT NULL""")
    op.execute("""UPDATE workbench_capability_selections s SET
        runtime=s.runtime || jsonb_build_object('status','completed','aborted',true),
        updated_at=now()
        WHERE s.runtime#>>'{method,call_id}' IN
            (SELECT call_id::text FROM workbench_method_authorizations)""")
    op.execute("""UPDATE workbench_interactions SET status='expired',
        response=jsonb_build_object('expired','method authorization semantics changed'),
        consumed_at=now()
        WHERE status='pending' AND id IN (
            SELECT interaction_id FROM workbench_calls
            WHERE status='awaiting_approval' AND detail#>>'{method,call_id}' IN
                (SELECT call_id::text FROM workbench_method_authorizations))""")
    op.execute("""UPDATE workbench_calls SET status='expired',updated_at=now()
        WHERE status='awaiting_approval' AND detail#>>'{method,call_id}' IN
            (SELECT call_id::text FROM workbench_method_authorizations)""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM workbench_method_authorizations) THEN
          RAISE EXCEPTION 'direct method grants require forward repair';
        END IF;
    END $$""")

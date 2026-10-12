"""Tenant consistency and write ownership for the memory tables.

1. Composite foreign keys keep every reference inside one organization, as Phases 03 and 05
   did: a memory's team and conversation, and a summary's or an extraction's conversation,
   must belong to the row's own organization. Deleting a conversation keeps the memories it
   produced but forgets the source (`ON DELETE SET NULL (conversation_id)` sets that column
   alone; PostgreSQL 15 or later). Deleting a team deletes its memories. Django's own
   single-column keys apply their actions in Python only; Runtime deletes with plain SQL,
   so two more keys make the database forget a deleted source message or successor itself.
2. Runtime extracts, retrieves and summarizes memories in the generation path, so
   `nexa_runtime` may write all four tables. The grant is skipped where the role does not
   exist (CI without the init script).
"""

from django.db import migrations

COMPOSITE_FKS = [
    (
        "ALTER TABLE memories ADD CONSTRAINT memories_conversation_same_org_fk "
        "FOREIGN KEY (organization_id, conversation_id) "
        "REFERENCES conversations (organization_id, id) ON DELETE SET NULL (conversation_id)",
        "ALTER TABLE memories DROP CONSTRAINT memories_conversation_same_org_fk",
    ),
    (
        "ALTER TABLE memories ADD CONSTRAINT memories_team_same_org_fk "
        "FOREIGN KEY (organization_id, team_id) "
        "REFERENCES teams (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE memories DROP CONSTRAINT memories_team_same_org_fk",
    ),
    (
        "ALTER TABLE conversation_summaries ADD CONSTRAINT conversation_summaries_same_org_fk "
        "FOREIGN KEY (organization_id, conversation_id) "
        "REFERENCES conversations (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE conversation_summaries DROP CONSTRAINT conversation_summaries_same_org_fk",
    ),
    (
        "ALTER TABLE memories ADD CONSTRAINT memories_source_message_set_null_fk "
        "FOREIGN KEY (source_message_id) REFERENCES messages (id) ON DELETE SET NULL",
        "ALTER TABLE memories DROP CONSTRAINT memories_source_message_set_null_fk",
    ),
    (
        "ALTER TABLE memories ADD CONSTRAINT memories_superseded_by_set_null_fk "
        "FOREIGN KEY (superseded_by_id) REFERENCES memories (id) ON DELETE SET NULL",
        "ALTER TABLE memories DROP CONSTRAINT memories_superseded_by_set_null_fk",
    ),
    (
        "ALTER TABLE memory_extractions ADD CONSTRAINT memory_extractions_same_org_fk "
        "FOREIGN KEY (organization_id, conversation_id) "
        "REFERENCES conversations (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE memory_extractions DROP CONSTRAINT memory_extractions_same_org_fk",
    ),
]

TABLES = "memories, memory_settings, conversation_summaries, memory_extractions"

GRANT_RUNTIME = f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLES} TO nexa_runtime;
    END IF;
END $$;
"""

REVOKE_RUNTIME = f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        REVOKE INSERT, UPDATE, DELETE ON {TABLES} FROM nexa_runtime;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("memory", "0001_initial"),
        ("chat", "0002_tenant_fk_and_runtime_grants"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        *[migrations.RunSQL(forward, backward) for forward, backward in COMPOSITE_FKS],
        migrations.RunSQL(GRANT_RUNTIME, REVOKE_RUNTIME),
    ]

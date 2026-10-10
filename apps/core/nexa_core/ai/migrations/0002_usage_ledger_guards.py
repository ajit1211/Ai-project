"""Database-level guarantees for the usage ledger.

1. Append-only for every role, including the owner: a trigger refuses UPDATE and DELETE.
   TRUNCATE (used by test teardown and, later, partition maintenance) is unaffected.
2. Runtime may insert rows: `nexa_runtime` gets INSERT on this one table. Roles are created
   outside migrations (scripts/db/init.sql locally, the secret store when deployed), so the
   grant is skipped where the role does not exist instead of failing the migration.
"""

from django.db import migrations

APPEND_ONLY = """
CREATE OR REPLACE FUNCTION ai_usage_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ai_usage_events is append-only (% refused)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ai_usage_events_append_only
    BEFORE UPDATE OR DELETE ON ai_usage_events
    FOR EACH ROW EXECUTE FUNCTION ai_usage_events_append_only();
"""

DROP_APPEND_ONLY = """
DROP TRIGGER IF EXISTS ai_usage_events_append_only ON ai_usage_events;
DROP FUNCTION IF EXISTS ai_usage_events_append_only();
"""

GRANT_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        GRANT SELECT, INSERT ON ai_usage_events TO nexa_runtime;
    END IF;
END $$;
"""

REVOKE_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        REVOKE INSERT ON ai_usage_events FROM nexa_runtime;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("ai", "0001_initial")]

    operations = [
        migrations.RunSQL(APPEND_ONLY, DROP_APPEND_ONLY),
        migrations.RunSQL(GRANT_RUNTIME, REVOKE_RUNTIME),
    ]

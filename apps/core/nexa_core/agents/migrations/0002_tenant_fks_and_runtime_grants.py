"""Tenant keys and Runtime's write access for agent runs (Phase 11, docs/architecture/agents.md).

1. Composite foreign keys keep a step in its run's organization and a tool call in its run's
   organization, cascading in the database: Core purges runs with plain deletes and their
   steps and tool calls go with them. (A run's agent is checked in the service instead: a
   platform agent has no organization, so it cannot be part of a composite key.)
2. Runtime claims and executes runs and writes their steps, so `nexa_runtime` may write
   `agent_runs` and `agent_steps`; agents themselves stay read-only for it (the default
   privileges already grant SELECT). The grants are skipped where the role does not exist.
"""

from django.db import migrations

COMPOSITE_FKS = [
    (
        "ALTER TABLE agent_steps ADD CONSTRAINT agent_steps_run_same_org_fk "
        "FOREIGN KEY (organization_id, run_id) "
        "REFERENCES agent_runs (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE agent_steps DROP CONSTRAINT agent_steps_run_same_org_fk",
    ),
    (
        "ALTER TABLE tool_invocations ADD CONSTRAINT tool_invocations_run_same_org_fk "
        "FOREIGN KEY (organization_id, run_id) "
        "REFERENCES agent_runs (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE tool_invocations DROP CONSTRAINT tool_invocations_run_same_org_fk",
    ),
]

GRANT_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON agent_runs, agent_steps TO nexa_runtime;
    END IF;
END $$;
"""

REVOKE_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        REVOKE INSERT, UPDATE ON agent_runs, agent_steps FROM nexa_runtime;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0001_initial"),
        ("tools", "0003_invocation_run"),
    ]

    operations = [
        *(migrations.RunSQL(forward, reverse) for forward, reverse in COMPOSITE_FKS),
        migrations.RunSQL(GRANT_RUNTIME, REVOKE_RUNTIME),
    ]

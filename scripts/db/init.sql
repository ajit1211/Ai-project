-- NEXA local database bootstrap (architecture doc 11.3).
--
-- Creates the `nexa` database and its two application roles:
--   nexa_core     owns the schema and runs every migration (Django)
--   nexa_runtime  reads Core's tables and writes only the tables Runtime owns (FastAPI)
--
-- LOCAL DEVELOPMENT AND CI ONLY: the passwords below match .env.example. Deployed
-- environments create these roles with secrets from AWS Secrets Manager.
--
-- Safe to run more than once. Run as a PostgreSQL superuser:
--   psql -h localhost -U postgres -f scripts/db/init.sql

SELECT 'CREATE ROLE nexa_core LOGIN CREATEDB PASSWORD ''nexa_core_dev'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_core')\gexec

SELECT 'CREATE ROLE nexa_runtime LOGIN PASSWORD ''nexa_runtime_dev'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime')\gexec

SELECT 'CREATE DATABASE nexa OWNER nexa_core'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'nexa')\gexec

\connect nexa

-- Runtime may read every table Core creates. Write grants are added table by table, in the
-- migration that creates a Runtime-owned table.
GRANT CONNECT ON DATABASE nexa TO nexa_runtime;
GRANT USAGE ON SCHEMA public TO nexa_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE nexa_core IN SCHEMA public
    GRANT SELECT ON TABLES TO nexa_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE nexa_core IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO nexa_runtime;

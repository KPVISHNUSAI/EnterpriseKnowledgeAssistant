-- One-time bootstrap of an Amazon RDS for PostgreSQL 17 instance. Run it as the RDS
-- master user, from a host that can reach the instance (see docs/DEPLOYMENT_AWS.md):
--
--   psql "host=<rds-endpoint> dbname=postgres user=<master-user> sslmode=require" \
--     -v ON_ERROR_STOP=1 -v owner_pw="$EKA_OWNER_PASSWORD" -v app_pw="$EKA_APP_PASSWORD" \
--     -f infra/aws/rds/bootstrap.sql
--
-- Mirrors infra/postgres/init/01-init.sh (local Docker), where the postgres user is a
-- superuser. The RDS master user is not, so it creates the database and the extension
-- itself and hands the database over to the owner role at the end.
--   eka_owner  owns the schema and runs the Alembic migrations (MIGRATION_DATABASE_URL)
--   eka_app    runtime role of the API and workers (DATABASE_URL): no superuser and no
--              BYPASSRLS, so row-level security applies to every query it runs

CREATE ROLE eka_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE eka_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Handing a database to another role requires membership in that role.
GRANT eka_owner TO CURRENT_USER;
CREATE DATABASE eka;

\connect eka

-- The migration's own CREATE EXTENSION IF NOT EXISTS is then a no-op for eka_owner.
CREATE EXTENSION IF NOT EXISTS vector;
GRANT CONNECT ON DATABASE eka TO eka_app;
GRANT USAGE ON SCHEMA public TO eka_app;

-- The database owner also owns the public schema (PostgreSQL 15+), so eka_owner can
-- create the tables.
ALTER DATABASE eka OWNER TO eka_owner;

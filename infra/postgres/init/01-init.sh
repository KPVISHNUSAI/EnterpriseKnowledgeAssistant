#!/bin/bash
# Runs once, as the postgres superuser, when the local database volume is first created.
# Creates two roles so row-level security is actually enforced:
#   eka_owner - owns the schema and runs Alembic migrations
#   eka_app   - runtime role used by the API and worker (no superuser, no BYPASSRLS)
# Also creates the eka_test database used by the backend test suite.
set -euo pipefail

: "${EKA_OWNER_PASSWORD:?EKA_OWNER_PASSWORD must be set}"
: "${EKA_APP_PASSWORD:?EKA_APP_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_pw="$EKA_OWNER_PASSWORD" -v app_pw="$EKA_APP_PASSWORD" <<-'EOSQL'
	CREATE ROLE eka_owner LOGIN PASSWORD :'owner_pw';
	CREATE ROLE eka_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
	CREATE DATABASE eka_test OWNER eka_owner;
EOSQL

for db in "$POSTGRES_DB" eka_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<-EOSQL
	ALTER DATABASE "$db" OWNER TO eka_owner;
	ALTER SCHEMA public OWNER TO eka_owner;
	CREATE EXTENSION IF NOT EXISTS vector;
	GRANT CONNECT ON DATABASE "$db" TO eka_app;
	GRANT USAGE ON SCHEMA public TO eka_app;
EOSQL
done

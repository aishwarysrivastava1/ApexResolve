#!/bin/sh
# Runs once, when the PostgreSQL volume is first created (docker-entrypoint-initdb.d).
# Creates the two roles and the database. Passwords come from the environment (.env); there are no defaults.
set -eu
: "${APEX_OWNER_PASSWORD:?APEX_OWNER_PASSWORD is required}"
: "${APEX_APP_PASSWORD:?APEX_APP_PASSWORD is required}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${POSTGRES_DB:-postgres}" \
     -v owner_pw="$APEX_OWNER_PASSWORD" -v app_pw="$APEX_APP_PASSWORD" <<'SQL'
CREATE ROLE apex_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE apex_app LOGIN PASSWORD :'app_pw';
CREATE DATABASE apex OWNER apex_owner;
REVOKE ALL ON DATABASE apex FROM PUBLIC;
GRANT CONNECT ON DATABASE apex TO apex_app;
SQL

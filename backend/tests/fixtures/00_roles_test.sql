-- TEST ONLY (embedded PostgreSQL in tests). Deployments use deploy/db-init/00_roles.sh with passwords from .env.
-- Passwords come from environment variables in real deployments; these are placeholders for local dev only.
CREATE ROLE apex_owner LOGIN PASSWORD 'change-me-owner';
CREATE ROLE apex_app   LOGIN PASSWORD 'change-me-app';
CREATE DATABASE apex OWNER apex_owner;

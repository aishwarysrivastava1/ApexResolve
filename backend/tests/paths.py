"""Shared paths for tests and scripts."""
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
POLICY_PATH = str(REPO / "policy" / "policy.2026.09.1.yaml")
ROLES_SQL = str(BACKEND / "tests" / "fixtures" / "00_roles_test.sql")   # test-only passwords
SCHEMA_SQL = str(BACKEND / "migrations" / "0001_initial.sql")
SEED_JSON = str(BACKEND / "seed" / "demo_seed.json")


def pg_host(server):
    """The `host` for connection URLs to a pgserver database: its socket directory on Linux and macOS,
    or "127.0.0.1:<port>" on Windows, where pgserver listens on TCP instead of a socket."""
    uri = server.get_uri()
    # 1. Linux/macOS: postgresql://postgres:@/postgres?host=/tmp/.../pg
    if "host=" in uri:
        return uri.split("host=")[1]
    # 2. Windows: postgresql://postgres:@127.0.0.1:54321/postgres
    return uri.split("@")[1].split("/")[0]

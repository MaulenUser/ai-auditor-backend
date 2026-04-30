from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bitrix_ingest.domain.tenant import Tenant  # noqa: E402
from bitrix_ingest.domain.user import User  # noqa: E402
from bitrix_ingest.infrastructure.database import TenantRepository, UserRepository  # noqa: E402


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,128}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROLES = {"admin", "client"}


def b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def hash_password(password: str) -> str:
    rounds = 260_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${b64_encode(digest)}"


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise SystemExit("Invalid username. Use 3-128 chars: letters, digits, dot, underscore, @, hyphen.")
    return username


def normalize_tenant_id(value: str) -> str:
    tenant_id = value.strip() or "default"
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise SystemExit("Invalid tenant id. Use 1-64 chars: letters, digits, underscore, hyphen.")
    return tenant_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update an AI Auditor backend user.")
    parser.add_argument("--username", required=True, help="Login, e.g. admin@example.com")
    parser.add_argument("--password", help="Password. If omitted, you will be prompted.")
    parser.add_argument("--tenant", default="default", help="Tenant id the user belongs to.")
    parser.add_argument("--tenant-name", default="", help="Human-readable tenant name.")
    parser.add_argument("--role", choices=sorted(ROLES), default="client", help="User role.")
    parser.add_argument("--inactive", action="store_true", help="Create the user as inactive.")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("BITRIX_DB_PATH", "data/app.db"),
        help="SQLite DB path when BITRIX_DATABASE_URL is not set.",
    )
    args = parser.parse_args()

    username = normalize_username(args.username)
    tenant_id = normalize_tenant_id(args.tenant)
    password = args.password or getpass.getpass("Password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")

    db_path = Path(args.db_path)
    tenants = TenantRepository(db_path)
    users = UserRepository(db_path)
    tenants.save(Tenant(id=tenant_id, name=args.tenant_name or tenant_id))
    users.save(
        User(
            username=username,
            tenant_id=tenant_id,
            role=args.role,
            password_hash=hash_password(password),
            active=not args.inactive,
        )
    )

    print(f"Saved user '{username}' with role '{args.role}' for tenant '{tenant_id}'.")


if __name__ == "__main__":
    main()

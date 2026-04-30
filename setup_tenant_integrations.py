"""Configure tenant integrations from the server side.

Usage:
    python setup_tenant_integrations.py --tenant sapaplast --name Sapaplast \
      --bitrix-webhook https://... --whatsapp-webhook https://... --openai-key sk-...

Values are stored in PostgreSQL when BITRIX_DATABASE_URL is set; otherwise
the local SQLite database configured by BITRIX_DB_PATH or data/app.db is used.
Secrets are never printed back to stdout.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bitrix_ingest.domain.integrations import Integrations  # noqa: E402
from bitrix_ingest.domain.tenant import Tenant  # noqa: E402
from bitrix_ingest.infrastructure.database import IntegrationsRepository, TenantRepository  # noqa: E402

_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure server-side tenant integrations.")
    parser.add_argument("--tenant", required=True, help="Tenant ID, for example sapaplast")
    parser.add_argument("--name", default="", help="Tenant display name")
    parser.add_argument("--bitrix-webhook", default="", help="Bitrix CRM webhook URL")
    parser.add_argument("--whatsapp-webhook", default="", help="WhatsApp Bitrix webhook URL")
    parser.add_argument("--openai-key", default="", help="OpenAI API key")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("BITRIX_DB_PATH", "data/app.db"),
        help="SQLite DB path when BITRIX_DATABASE_URL is not set.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    tenant_id = args.tenant.strip()
    if not _TENANT_ID_RE.fullmatch(tenant_id):
        raise SystemExit("Invalid --tenant. Use 1-64 characters: letters, digits, underscore, hyphen.")

    db_path = Path(args.db_path)
    tenants = TenantRepository(db_path)
    tenants.save(Tenant(id=tenant_id, name=args.name.strip() or tenant_id))

    current = IntegrationsRepository(db_path, tenant_id).get() or Integrations()
    integrations = Integrations(
        bitrix_webhook_url=args.bitrix_webhook.strip() or current.bitrix_webhook_url,
        whatsapp_webhook_url=args.whatsapp_webhook.strip() or current.whatsapp_webhook_url,
        openai_api_key=args.openai_key.strip() or current.openai_api_key,
    )
    IntegrationsRepository(db_path, tenant_id).save(integrations)

    status = integrations.to_status_dict()
    print(f"tenant={tenant_id}")
    print(f"bitrix_webhook_url_configured={status['bitrix_webhook_url_configured']}")
    print(f"whatsapp_webhook_url_configured={status['whatsapp_webhook_url_configured']}")
    print(f"openai_api_key_configured={status['openai_api_key_configured']}")


if __name__ == "__main__":
    main()

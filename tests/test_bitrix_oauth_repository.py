from __future__ import annotations

from bitrix_ingest.domain.bitrix_oauth import BitrixOAuthToken
from bitrix_ingest.infrastructure.database import BitrixOAuthRepository


def _token(**overrides) -> BitrixOAuthToken:
    data = {
        "tenant_id": "tenant-bitrix-1",
        "bitrix_member_id": "member-123",
        "bitrix_domain": "client.bitrix24.kz",
        "client_endpoint": "https://client.bitrix24.kz/rest/",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_at": 1_800_000_000,
        "scope": "crm,user_basic,task",
        "status": "active",
    }
    data.update(overrides)
    return BitrixOAuthToken(**data)


def test_save_and_get_bitrix_oauth_token_by_tenant(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")

    repo.save(_token())

    saved = repo.get_by_tenant("tenant-bitrix-1")
    assert saved is not None
    assert saved.bitrix_member_id == "member-123"
    assert saved.bitrix_domain == "client.bitrix24.kz"
    assert saved.client_endpoint == "https://client.bitrix24.kz/rest/"
    assert saved.access_token == "access-token"
    assert saved.refresh_token == "refresh-token"
    assert saved.expires_at == 1_800_000_000
    assert saved.scope == "crm,user_basic,task"
    assert saved.status == "active"


def test_get_bitrix_oauth_token_by_member_id(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token(bitrix_member_id="member-abc"))

    saved = repo.get_by_member_id("member-abc")

    assert saved is not None
    assert saved.tenant_id == "tenant-bitrix-1"


def test_upsert_bitrix_oauth_token_refreshes_secret_fields(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token(access_token="old-access", refresh_token="old-refresh"))

    repo.save(
        _token(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=1_900_000_000,
            scope="crm,user_basic,task,imopenlines",
        )
    )

    saved = repo.get_by_tenant("tenant-bitrix-1")
    assert saved is not None
    assert saved.access_token == "new-access"
    assert saved.refresh_token == "new-refresh"
    assert saved.expires_at == 1_900_000_000
    assert saved.scope == "crm,user_basic,task,imopenlines"


def test_status_dict_does_not_expose_oauth_tokens(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token())

    status = repo.get_by_tenant("tenant-bitrix-1").to_status_dict()

    assert status["configured"] is True
    assert status["bitrix_member_id"] == "member-123"
    assert "access_token" not in status
    assert "refresh_token" not in status


def test_update_status_and_delete_bitrix_oauth_token(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token())

    repo.update_status("tenant-bitrix-1", "revoked")
    assert repo.get_by_tenant("tenant-bitrix-1").status == "revoked"

    repo.delete_by_tenant("tenant-bitrix-1")
    assert repo.get_by_tenant("tenant-bitrix-1") is None

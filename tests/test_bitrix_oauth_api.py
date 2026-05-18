from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from bitrix_ingest.api import app as app_module
from bitrix_ingest.domain.integrations import Integrations
from bitrix_ingest.domain.tenant import Tenant
from bitrix_ingest.domain.user import User
from bitrix_ingest.infrastructure.database import BitrixOAuthRepository, TenantRepository, UserRepository
from bitrix_ingest.infrastructure.http import BitrixClient, BitrixOAuthClient

REQUIRED_SCOPES = ",".join(app_module._REQUIRED_BITRIX_OAUTH_SCOPES)


def _client(tmp_path, monkeypatch, *, auth_required: bool = True) -> TestClient:
    monkeypatch.setattr(app_module, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("AI_AUDITOR_AUTH_REQUIRED", "true" if auth_required else "false")
    monkeypatch.setenv("AI_AUDITOR_AUTH_SECRET", "test-auth-secret")
    return TestClient(app_module.app)


def _save_user(tmp_path, username: str, tenant_id: str, role: str = "client") -> None:
    db_path = tmp_path / "app.db"
    TenantRepository(db_path).save(Tenant(id=tenant_id, name=tenant_id))
    UserRepository(db_path).save(
        User(
            username=username,
            tenant_id=tenant_id,
            role=role,
            password_hash=app_module._hash_password("secret-password"),
            active=True,
        )
    )


def _login(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "secret-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_bitrix_install_callback_saves_oauth_token_and_creates_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_APPLICATION_TOKEN", "app-token")

    response = client.post(
        "/api/bitrix/install",
        data={
            "event": "ONAPPINSTALL",
            "auth[access_token]": "access-token",
            "auth[refresh_token]": "refresh-token",
            "auth[expires_in]": "3600",
            "auth[scope]": "crm,user_basic,task",
            "auth[domain]": "client.bitrix24.kz",
            "auth[client_endpoint]": "https://client.bitrix24.kz/rest/",
            "auth[member_id]": "member-123",
            "auth[application_token]": "app-token",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tenant_id"] == "member-123"
    assert body["bitrix_oauth"]["configured"] is True
    assert "access_token" not in body["bitrix_oauth"]
    assert "refresh_token" not in body["bitrix_oauth"]

    token = BitrixOAuthRepository(tmp_path / "app.db").get_by_tenant("member-123")
    assert token is not None
    assert token.access_token == "access-token"
    assert token.refresh_token == "refresh-token"
    assert token.bitrix_domain == "client.bitrix24.kz"
    assert token.client_endpoint == "https://client.bitrix24.kz/rest/"
    assert token.scope == "crm,user_basic,task"
    assert token.expires_at > int(time.time())
    assert TenantRepository(tmp_path / "app.db").get("member-123").name == "client.bitrix24.kz"


def test_bitrix_install_callback_preserves_existing_tenant_binding(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    db_path = tmp_path / "app.db"
    TenantRepository(db_path).save(Tenant(id="client-tenant", name="Client Tenant"))
    repo = BitrixOAuthRepository(db_path)
    repo.save(
        app_module.BitrixOAuthToken(
            tenant_id="client-tenant",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="old-access",
            refresh_token="old-refresh",
            status="active",
        )
    )

    response = client.post(
        "/api/bitrix/install",
        json={
            "auth": {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": "3600",
                "scope": "crm,user_basic,task",
                "domain": "client.bitrix24.kz",
                "client_endpoint": "https://client.bitrix24.kz/rest/",
                "member_id": "member-123",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "client-tenant"
    token = repo.get_by_tenant("client-tenant")
    assert token is not None
    assert token.access_token == "new-access"
    assert token.refresh_token == "new-refresh"
    assert repo.get_by_tenant("member-123") is None


def test_bitrix_install_rejects_invalid_application_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_APPLICATION_TOKEN", "expected-token")

    response = client.post(
        "/api/bitrix/install",
        json={
            "auth": {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "member_id": "member-123",
                "domain": "client.bitrix24.kz",
                "client_endpoint": "https://client.bitrix24.kz/rest/",
                "application_token": "wrong-token",
            }
        },
    )

    assert response.status_code == 403


def test_bitrix_oauth_start_redirects_to_portal_with_signed_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    _save_user(tmp_path, "client@example.com", "client-tenant")
    token = _login(client, "client@example.com")

    response = client.get(
        "/api/bitrix/oauth/start",
        params={"portal": "https://client.bitrix24.kz/", "return_url": "/app/#/settings"},
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "https"
    assert parsed.netloc == "client.bitrix24.kz"
    assert parsed.path == "/oauth/authorize/"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["client-id"]
    assert query["response_type"] == ["code"]
    state = app_module._decode_bitrix_oauth_state(query["state"][0])
    assert state["portal"] == "client.bitrix24.kz"
    assert state["return_url"] == "/app/#/settings"
    assert state["tenant_id"] == "client-tenant"


def test_bitrix_connect_start_returns_authorize_url_for_frontend(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    _save_user(tmp_path, "client@example.com", "client-tenant")
    token = _login(client, "client@example.com")

    response = client.post(
        "/api/bitrix/connect/start",
        json={"portal": "https://client.bitrix24.kz/", "return_url": "/app/#/settings"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tenant_id"] == "client-tenant"
    assert body["portal"] == "client.bitrix24.kz"
    assert body["return_url"] == "/app/#/settings"
    assert body["bitrix_oauth"]["configured"] is False

    parsed = urlparse(body["authorize_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "client.bitrix24.kz"
    assert parsed.path == "/oauth/authorize/"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["client-id"]
    assert query["response_type"] == ["code"]
    state = app_module._decode_bitrix_oauth_state(query["state"][0])
    assert state["portal"] == "client.bitrix24.kz"
    assert state["return_url"] == "/app/#/settings"
    assert state["tenant_id"] == "client-tenant"


def test_bitrix_oauth_start_requires_login(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")

    response = client.get(
        "/api/bitrix/oauth/start",
        params={"portal": "client.bitrix24.kz"},
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_bitrix_connect_start_requires_login(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")

    response = client.post(
        "/api/bitrix/connect/start",
        json={"portal": "client.bitrix24.kz"},
    )

    assert response.status_code == 401


def test_bitrix_oauth_callback_exchanges_code_and_saves_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_SECRET", "client-secret")
    state = app_module._create_bitrix_oauth_state("client.bitrix24.kz")

    calls = {}

    def fake_exchange(params):
        calls.update(params)
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "crm,user_basic,task",
            "domain": "client.bitrix24.kz",
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
        }

    monkeypatch.setattr(app_module, "_request_bitrix_oauth_token", fake_exchange)

    response = client.get(
        "/api/bitrix/oauth/callback",
        params={"code": "auth-code", "state": state, "domain": "client.bitrix24.kz"},
    )

    assert response.status_code == 200
    assert calls == {
        "grant_type": "authorization_code",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "code": "auth-code",
    }
    body = response.json()
    assert body["tenant_id"] == "member-123"
    assert body["bitrix_oauth"]["configured"] is True
    assert BitrixOAuthRepository(tmp_path / "app.db").get_by_member_id("member-123") is not None


def test_bitrix_oauth_callback_binds_token_to_state_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_SECRET", "client-secret")
    TenantRepository(tmp_path / "app.db").save(Tenant(id="client-tenant", name="Client Tenant"))
    state = app_module._create_bitrix_oauth_state(
        "client.bitrix24.kz",
        "/app/#/settings",
        tenant_id="client-tenant",
    )

    def fake_exchange(params):
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "crm,user_basic,task,department",
            "domain": "client.bitrix24.kz",
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
        }

    monkeypatch.setattr(app_module, "_request_bitrix_oauth_token", fake_exchange)

    response = client.get(
        "/api/bitrix/oauth/callback",
        params={"code": "auth-code", "state": state, "domain": "client.bitrix24.kz"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    body_location = response.headers["location"]
    assert "bitrix_oauth=connected" in body_location
    assert "tenant_id=client-tenant" in body_location

    repo = BitrixOAuthRepository(tmp_path / "app.db")
    token = repo.get_by_tenant("client-tenant")
    assert token is not None
    assert token.bitrix_member_id == "member-123"
    assert token.access_token == "access-token"
    assert repo.get_by_tenant("member-123") is None
    assert TenantRepository(tmp_path / "app.db").get("client-tenant").name == "Client Tenant"


def test_bitrix_oauth_callback_prefers_callback_scope_when_token_scope_is_app(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_SECRET", "client-secret")
    TenantRepository(tmp_path / "app.db").save(Tenant(id="client-tenant", name="Client Tenant"))
    state = app_module._create_bitrix_oauth_state("client.bitrix24.kz", tenant_id="client-tenant")

    def fake_exchange(params):
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "app",
            "domain": "client.bitrix24.kz",
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
        }

    monkeypatch.setattr(app_module, "_request_bitrix_oauth_token", fake_exchange)

    response = client.get(
        "/api/bitrix/oauth/callback",
        params={
            "code": "auth-code",
            "state": state,
            "domain": "client.bitrix24.kz",
            "scope": REQUIRED_SCOPES,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bitrix_oauth"]["scope"] == REQUIRED_SCOPES
    assert body["bitrix_oauth"]["missing_scopes"] == []
    assert body["bitrix_oauth"]["has_required_scopes"] is True
    token = BitrixOAuthRepository(tmp_path / "app.db").get_by_tenant("client-tenant")
    assert token.scope == REQUIRED_SCOPES


def test_bitrix_oauth_callback_rebinds_existing_member_token_to_state_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("BITRIX_OAUTH_CLIENT_SECRET", "client-secret")
    db_path = tmp_path / "app.db"
    TenantRepository(db_path).save(Tenant(id="client-tenant", name="Client Tenant"))
    repo = BitrixOAuthRepository(db_path)
    repo.save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="old-access",
            refresh_token="old-refresh",
            status="active",
        )
    )
    state = app_module._create_bitrix_oauth_state("client.bitrix24.kz", tenant_id="client-tenant")

    def fake_exchange(params):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "scope": "crm,user_basic,task,department",
            "domain": "client.bitrix24.kz",
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
        }

    monkeypatch.setattr(app_module, "_request_bitrix_oauth_token", fake_exchange)

    response = client.get(
        "/api/bitrix/oauth/callback",
        params={"code": "auth-code", "state": state, "domain": "client.bitrix24.kz"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "client-tenant"
    assert repo.get_by_tenant("member-123") is None
    rebound = repo.get_by_tenant("client-tenant")
    assert rebound is not None
    assert rebound.bitrix_member_id == "member-123"
    assert rebound.access_token == "new-access"


def test_bitrix_uninstall_marks_token_revoked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=True)
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            scope=REQUIRED_SCOPES,
            status="active",
        )
    )

    response = client.post(
        "/api/bitrix/uninstall",
        json={"auth": {"member_id": "member-123"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "member-123"
    assert body["bitrix_oauth"]["status"] == "revoked"
    assert repo.get_by_tenant("member-123").status == "revoked"


def test_get_integrations_includes_bitrix_oauth_status_without_secrets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth_required=False)
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            status="active",
        )
    )

    response = client.get("/api/integrations", headers={"X-Tenant-Id": "member-123"})

    assert response.status_code == 200
    body = response.json()
    assert body["bitrix_oauth"]["configured"] is True
    assert body["bitrix_oauth"]["bitrix_member_id"] == "member-123"
    assert body["bitrix_oauth"]["required_scopes"] == list(app_module._REQUIRED_BITRIX_OAUTH_SCOPES)
    assert body["bitrix_oauth"]["missing_scopes"] == list(app_module._REQUIRED_BITRIX_OAUTH_SCOPES)
    assert body["bitrix_oauth"]["has_required_scopes"] is False
    assert body["bitrix_oauth"]["configuration_error"].startswith("Недостаточно прав Bitrix")
    assert "access_token" not in body["bitrix_oauth"]
    assert "refresh_token" not in body["bitrix_oauth"]


def test_bitrix_gateway_prefers_request_webhook_over_oauth(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, auth_required=False)
    BitrixOAuthRepository(tmp_path / "app.db").save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            scope=REQUIRED_SCOPES,
            status="active",
        )
    )

    gateway = app_module._resolve_bitrix_gateway(
        "https://override.bitrix24.kz/rest/1/webhook/",
        "member-123",
    )

    assert isinstance(gateway, BitrixClient)
    assert gateway.base_url == "https://override.bitrix24.kz/rest/1/webhook/"


def test_bitrix_gateway_uses_oauth_before_stored_webhook(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, auth_required=False)
    app_module._integrations_repo("member-123").save(
        Integrations(bitrix_webhook_url="https://legacy.bitrix24.kz/rest/1/webhook/")
    )
    BitrixOAuthRepository(tmp_path / "app.db").save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            scope=REQUIRED_SCOPES,
            status="active",
        )
    )

    gateway = app_module._resolve_bitrix_gateway(None, "member-123")

    assert isinstance(gateway, BitrixOAuthClient)


def test_bitrix_gateway_rejects_oauth_missing_required_scopes(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, auth_required=False)
    BitrixOAuthRepository(tmp_path / "app.db").save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            scope="crm,task,user_basic,user,imopenlines,telephony,disk",
            status="active",
        )
    )

    with pytest.raises(app_module.HTTPException) as exc:
        app_module._resolve_bitrix_gateway(None, "member-123")

    assert exc.value.status_code == 422
    assert exc.value.detail["missing_scopes"] == ["department"]
    assert exc.value.detail["message"] == "Недостаточно прав Bitrix: отсутствует department"


def test_bitrix_gateway_falls_back_to_stored_webhook(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, auth_required=False)
    app_module._integrations_repo("legacy").save(
        Integrations(bitrix_webhook_url="https://legacy.bitrix24.kz/rest/1/webhook/")
    )

    gateway = app_module._resolve_bitrix_gateway(None, "legacy")

    assert isinstance(gateway, BitrixClient)
    assert gateway.base_url == "https://legacy.bitrix24.kz/rest/1/webhook/"


def test_whatsapp_gateway_uses_oauth_when_no_whatsapp_webhook(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, auth_required=False)
    BitrixOAuthRepository(tmp_path / "app.db").save(
        app_module.BitrixOAuthToken(
            tenant_id="member-123",
            bitrix_member_id="member-123",
            bitrix_domain="client.bitrix24.kz",
            client_endpoint="https://client.bitrix24.kz/rest/",
            access_token="access-token",
            refresh_token="refresh-token",
            scope=REQUIRED_SCOPES,
            status="active",
        )
    )

    gateway = app_module._resolve_whatsapp_gateway(None, "member-123")

    assert isinstance(gateway, BitrixOAuthClient)

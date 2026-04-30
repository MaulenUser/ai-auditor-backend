from __future__ import annotations

from fastapi.testclient import TestClient

from bitrix_ingest.api import app as app_module
from bitrix_ingest.domain.tenant import Tenant
from bitrix_ingest.domain.user import User
from bitrix_ingest.infrastructure.database import TenantRepository, UserRepository


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_module, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("AI_AUDITOR_AUTH_REQUIRED", "true")
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


def test_auth_required_blocks_unauthenticated_app_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/app-state")

    assert response.status_code == 401


def test_client_token_forces_own_tenant(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save_user(tmp_path, "client@example.com", "tenant-a")
    token = _login(client, "client@example.com")

    own = client.get("/api/app-state", headers={"Authorization": f"Bearer {token}"})
    forbidden = client.get(
        "/api/app-state",
        headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": "tenant-b"},
    )

    assert own.status_code == 200
    assert own.json()["tenant_id"] == "tenant-a"
    assert forbidden.status_code == 403


def test_admin_can_choose_tenant_and_manage_tenants(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save_user(tmp_path, "admin@example.com", "default", role="admin")
    token = _login(client, "admin@example.com")

    create = client.post(
        "/api/tenants",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": "tenant-b", "name": "Tenant B"},
    )
    state = client.get(
        "/api/app-state",
        headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": "tenant-b"},
    )

    assert create.status_code == 200
    assert state.status_code == 200
    assert state.json()["tenant_id"] == "tenant-b"

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from bitrix_ingest.domain.bitrix_oauth import BitrixOAuthToken
from bitrix_ingest.domain.exceptions import BitrixError
from bitrix_ingest.infrastructure.database import BitrixOAuthRepository
from bitrix_ingest.infrastructure.http import BitrixOAuthClient


def _response(data: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = data
    response.raise_for_status.return_value = None
    response.text = str(data)
    return response


def _http_error(status_code: int, body: str = "error") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    error = requests.HTTPError(response=response)

    failed = MagicMock()
    failed.raise_for_status.side_effect = error
    failed.text = body
    return failed


def _token(**overrides) -> BitrixOAuthToken:
    data = {
        "tenant_id": "tenant-oauth",
        "bitrix_member_id": "member-123",
        "bitrix_domain": "client.bitrix24.kz",
        "client_endpoint": "https://client.bitrix24.kz/rest/",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_at": int(time.time()) + 3600,
        "scope": "crm,user_basic,task",
        "status": "active",
    }
    data.update(overrides)
    return BitrixOAuthToken(**data)


def _client(tmp_path, session: MagicMock, **kwargs) -> BitrixOAuthClient:
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    if kwargs.pop("save_token", True):
        repo.save(_token(**kwargs.pop("token_overrides", {})))
    return BitrixOAuthClient(
        "tenant-oauth",
        repo,
        client_id="client-id",
        client_secret="client-secret",
        session=session,
        max_attempts=2,
        **kwargs,
    )


def test_oauth_client_calls_bitrix_rest_with_access_token(tmp_path):
    session = MagicMock()
    session.post.return_value = _response({"result": {"ID": "1"}})
    client = _client(tmp_path, session)

    result = client.call("crm.deal.get", body={"id": 1})

    assert result == {"result": {"ID": "1"}}
    session.get.assert_not_called()
    session.post.assert_called_once()
    assert session.post.call_args[0][0] == "https://client.bitrix24.kz/rest/crm.deal.get.json"
    assert session.post.call_args[1]["json"] == {"id": 1, "auth": "access-token"}


def test_oauth_client_refreshes_stale_token_before_call(tmp_path):
    session = MagicMock()
    session.get.return_value = _response(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
            "scope": "crm,user_basic,task,imopenlines",
        }
    )
    session.post.return_value = _response({"result": []})
    client = _client(tmp_path, session, token_overrides={"expires_at": 1})

    client.call("crm.deal.list")

    session.get.assert_called_once()
    assert session.get.call_args[0][0] == "https://oauth.bitrix.info/oauth/token/"
    assert session.get.call_args[1]["params"] == {
        "grant_type": "refresh_token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }
    assert session.post.call_args[1]["json"]["auth"] == "new-access"

    saved = BitrixOAuthRepository(tmp_path / "app.db").get_by_tenant("tenant-oauth")
    assert saved.access_token == "new-access"
    assert saved.refresh_token == "new-refresh"
    assert saved.scope == "crm,user_basic,task,imopenlines"


def test_oauth_client_refreshes_and_retries_on_expired_token_response(tmp_path):
    session = MagicMock()
    session.post.side_effect = [
        _response({"error": "expired_token", "error_description": "expired"}),
        _response({"result": "ok"}),
    ]
    session.get.return_value = _response(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires": int(time.time()) + 3600,
            "client_endpoint": "https://client.bitrix24.kz/rest/",
            "member_id": "member-123",
        }
    )
    client = _client(tmp_path, session)

    result = client.call("profile")

    assert result == {"result": "ok"}
    assert session.post.call_count == 2
    assert session.post.call_args_list[0][1]["json"]["auth"] == "access-token"
    assert session.post.call_args_list[1][1]["json"]["auth"] == "new-access"


def test_oauth_client_list_all_uses_oauth_call(tmp_path):
    session = MagicMock()
    session.post.side_effect = [
        _response({"result": [{"ID": "1"}], "next": 50}),
        _response({"result": [{"ID": "2"}]}),
    ]
    client = _client(tmp_path, session)

    rows = client.list_all("crm.deal.list", select=["ID"])

    assert rows == [{"ID": "1"}, {"ID": "2"}]
    assert session.post.call_count == 2
    assert session.post.call_args_list[0][1]["json"]["auth"] == "access-token"
    assert session.post.call_args_list[1][1]["json"]["start"] == 50


def test_oauth_client_raises_when_token_missing(tmp_path):
    session = MagicMock()
    client = _client(tmp_path, session, save_token=False)

    with pytest.raises(BitrixError, match="not configured"):
        client.call("profile")


def test_oauth_client_raises_when_refresh_credentials_missing(tmp_path):
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token(expires_at=1))
    client = BitrixOAuthClient("tenant-oauth", repo, session=MagicMock())

    with pytest.raises(BitrixError, match="credentials"):
        client.call("profile")


def test_oauth_client_marks_token_error_on_refresh_error_response(tmp_path):
    session = MagicMock()
    session.get.return_value = _response(
        {"error": "invalid_grant", "error_description": "refresh token expired"}
    )
    repo = BitrixOAuthRepository(tmp_path / "app.db")
    repo.save(_token(expires_at=1))
    client = BitrixOAuthClient(
        "tenant-oauth",
        repo,
        client_id="client-id",
        client_secret="client-secret",
        session=session,
    )

    with pytest.raises(BitrixError, match="invalid_grant"):
        client.call("profile")

    assert repo.get_by_tenant("tenant-oauth").status == "error"


def test_oauth_client_retries_refresh_transient_http_error(tmp_path):
    session = MagicMock()
    session.get.side_effect = [
        _http_error(503, "temporarily unavailable"),
        _response(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "client_endpoint": "https://client.bitrix24.kz/rest/",
                "member_id": "member-123",
            }
        ),
    ]
    session.post.return_value = _response({"result": "ok"})
    client = _client(tmp_path, session, token_overrides={"expires_at": 1})

    with patch("time.sleep"):
        result = client.call("profile")

    assert result == {"result": "ok"}
    assert session.get.call_count == 2
    assert session.post.call_args[1]["json"]["auth"] == "new-access"

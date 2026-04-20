"""Tests for BitrixClient: retries, pagination, URL normalisation."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from bitrix_ingest.domain.exceptions import BitrixError
from bitrix_ingest.infrastructure.http import BitrixClient, WebhookUrl


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def test_normalize_adds_slash():
    assert str(WebhookUrl.parse("https://x.bitrix24.ru/rest/1/token")) == "https://x.bitrix24.ru/rest/1/token/"


def test_normalize_keeps_existing_slash():
    assert str(WebhookUrl.parse("https://x.bitrix24.ru/rest/1/token/")) == "https://x.bitrix24.ru/rest/1/token/"


def test_normalize_strips_whitespace():
    assert str(WebhookUrl.parse("  https://x.bitrix24.ru/rest/  ")) == "https://x.bitrix24.ru/rest/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ok_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _make_http_error(status_code: int) -> requests.HTTPError:
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.text = f"HTTP {status_code}"
    return requests.HTTPError(response=fake_resp)


def _client_with_session(session: MagicMock) -> BitrixClient:
    return BitrixClient("https://x.bitrix24.ru/rest/1/token/", session=session)


# ---------------------------------------------------------------------------
# call() — happy path
# ---------------------------------------------------------------------------

class TestCall:
    def test_successful_call_returns_json(self):
        session = MagicMock()
        session.post.return_value = _make_ok_response({"result": {"ID": "1"}})
        client = _client_with_session(session)

        result = client.call("profile")

        assert result == {"result": {"ID": "1"}}
        session.post.assert_called_once()
        url = session.post.call_args[0][0]
        assert url == "https://x.bitrix24.ru/rest/1/token/profile.json"

    def test_body_sent_as_json(self):
        session = MagicMock()
        session.post.return_value = _make_ok_response({"result": []})
        client = _client_with_session(session)

        client.call("crm.deal.list", body={"select": ["ID"], "start": 0})

        _, kwargs = session.post.call_args
        assert kwargs["json"] == {"select": ["ID"], "start": 0}


# ---------------------------------------------------------------------------
# call() — retry behaviour
# ---------------------------------------------------------------------------

class TestRetries:
    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    def test_retries_on_transient_http_error(self, status_code: int):
        session = MagicMock()
        error_resp = MagicMock()
        error_resp.status_code = status_code
        error_resp.text = "error"

        def raise_first_then_ok(*args, **kwargs):
            if session.post.call_count == 1:
                r = MagicMock()
                r.raise_for_status.side_effect = requests.HTTPError(response=error_resp)
                return r
            return _make_ok_response({"result": "ok"})

        session.post.side_effect = raise_first_then_ok
        client = _client_with_session(session)

        with patch("time.sleep"):
            result = client.call("some.method")

        assert result == {"result": "ok"}
        assert session.post.call_count == 2

    def test_non_transient_error_raises_immediately(self):
        session = MagicMock()
        error_resp = MagicMock()
        error_resp.status_code = 400
        error_resp.text = "Bad Request"

        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError(response=error_resp)
        session.post.return_value = resp
        client = _client_with_session(session)

        with pytest.raises(BitrixError):
            client.call("some.method")

        assert session.post.call_count == 1  # no retries

    def test_exhausts_all_attempts_and_raises(self):
        session = MagicMock()
        error_resp = MagicMock()
        error_resp.status_code = 503
        error_resp.text = "Service Unavailable"

        def always_fail(*args, **kwargs):
            r = MagicMock()
            r.raise_for_status.side_effect = requests.HTTPError(response=error_resp)
            return r

        session.post.side_effect = always_fail
        client = BitrixClient("https://x.bitrix24.ru/rest/1/t/", max_attempts=3, session=session)

        with patch("time.sleep"), pytest.raises(BitrixError):
            client.call("some.method")

        assert session.post.call_count == 3

    def test_retries_on_connection_timeout(self):
        session = MagicMock()

        def raise_first_then_ok(*args, **kwargs):
            if session.post.call_count == 1:
                raise requests.ConnectionError("timed out")
            return _make_ok_response({"result": "ok"})

        session.post.side_effect = raise_first_then_ok
        client = _client_with_session(session)

        with patch("time.sleep"):
            result = client.call("some.method")

        assert result == {"result": "ok"}
        assert session.post.call_count == 2

    @pytest.mark.parametrize(
        "message",
        [
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
            "Remote end closed connection without response",
        ],
    )
    def test_retries_on_remote_disconnect_connection_errors(self, message: str):
        session = MagicMock()

        def raise_first_then_ok(*args, **kwargs):
            if session.post.call_count == 1:
                raise requests.ConnectionError(message)
            return _make_ok_response({"result": "ok"})

        session.post.side_effect = raise_first_then_ok
        client = _client_with_session(session)

        with patch("time.sleep"):
            result = client.call("some.method")

        assert result == {"result": "ok"}
        assert session.post.call_count == 2

    def test_non_transient_connection_error_raises_immediately(self):
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("SSL certificate verify failed")
        client = _client_with_session(session)

        with pytest.raises(BitrixError):
            client.call("some.method")

        assert session.post.call_count == 1


# ---------------------------------------------------------------------------
# list_all() — pagination
# ---------------------------------------------------------------------------

class TestListAll:
    def test_single_page_no_next(self):
        session = MagicMock()
        session.post.return_value = _make_ok_response({
            "result": [{"ID": "1"}, {"ID": "2"}],
            # no "next" key
        })
        client = _client_with_session(session)

        items = client.list_all("crm.deal.list", select=["ID"])

        assert items == [{"ID": "1"}, {"ID": "2"}]
        assert session.post.call_count == 1

    def test_two_pages(self):
        session = MagicMock()
        page1 = _make_ok_response({"result": [{"ID": str(i)} for i in range(50)], "next": 50})
        page2 = _make_ok_response({"result": [{"ID": "50"}]})
        session.post.side_effect = [page1, page2]
        client = _client_with_session(session)

        items = client.list_all("crm.deal.list", select=["ID"])

        assert len(items) == 51
        assert session.post.call_count == 2
        # second call must use start=50
        second_body = session.post.call_args_list[1][1]["json"]
        assert second_body["start"] == 50

    def test_empty_result_stops_pagination(self):
        session = MagicMock()
        session.post.return_value = _make_ok_response({"result": None})
        client = _client_with_session(session)

        items = client.list_all("crm.deal.list", select=["ID"])

        assert items == []
        assert session.post.call_count == 1

    def test_limit_stops_after_first_page(self):
        session = MagicMock()
        page1 = _make_ok_response({"result": [{"ID": str(i)} for i in range(50)], "next": 50})
        page2 = _make_ok_response({"result": [{"ID": "50"}]})
        session.post.side_effect = [page1, page2]
        client = _client_with_session(session)

        items = client.list_all("crm.deal.list", select=["ID"], limit=10)

        assert len(items) == 10
        assert items[0]["ID"] == "0"
        assert items[-1]["ID"] == "9"
        assert session.post.call_count == 1

    def test_limit_can_span_multiple_pages(self):
        session = MagicMock()
        page1 = _make_ok_response({"result": [{"ID": str(i)} for i in range(50)], "next": 50})
        page2 = _make_ok_response({"result": [{"ID": str(i)} for i in range(50, 100)]})
        session.post.side_effect = [page1, page2]
        client = _client_with_session(session)

        items = client.list_all("crm.deal.list", select=["ID"], limit=60)

        assert len(items) == 60
        assert items[0]["ID"] == "0"
        assert items[-1]["ID"] == "59"
        assert session.post.call_count == 2

    def test_page_delay_is_honoured(self):
        session = MagicMock()
        page1 = _make_ok_response({"result": [{"ID": "1"}], "next": 50})
        page2 = _make_ok_response({"result": [{"ID": "2"}]})
        session.post.side_effect = [page1, page2]
        client = BitrixClient("https://x.bitrix24.ru/rest/1/t/", page_delay=0.5, session=session)

        with patch("time.sleep") as sleep_mock:
            client.list_all("crm.deal.list", select=["ID"])

        sleep_mock.assert_called_once_with(0.5)

    def test_filter_and_order_forwarded(self):
        session = MagicMock()
        session.post.return_value = _make_ok_response({"result": []})
        client = _client_with_session(session)

        client.list_all(
            "crm.deal.list",
            select=["ID"],
            filter={">=DATE_MODIFY": "2024-01-01"},
            order={"DATE_MODIFY": "DESC"},
        )

        body = session.post.call_args[1]["json"]
        assert body["filter"] == {">=DATE_MODIFY": "2024-01-01"}
        assert body["order"] == {"DATE_MODIFY": "DESC"}

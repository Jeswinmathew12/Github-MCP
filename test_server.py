from unittest.mock import patch, MagicMock

import pytest
import requests

import server

ONE_ISSUE = [
    {"number": 1, "title": "Bug", "user": {"login": "alice"},
     "labels": [], "created_at": "2024-01-01T00:00:00Z", "html_url": "x"}
]


@pytest.fixture(autouse=True)
def no_real_sleeping():
    """Keep the retry tests instant; also lets them assert on the backoff."""
    with patch("server.time.sleep") as sleep:
        yield sleep


def _mock_response(status_code, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.text = str(json_data)
    return resp

@patch("server.requests.get")
def test_list_open_issues_success(mock_get):
    mock_get.return_value = _mock_response(200, ONE_ISSUE)
    result = server.list_open_issues("owner/repo", limit=5)
    assert result["count"] == 1
    assert result["open_issues"][0]["author"] == "alice"

@patch("server.requests.get")
def test_rate_limit_returns_readable_error(mock_get):
    mock_get.return_value = _mock_response(
        403, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}
    )
    result = server.list_open_issues("owner/repo")
    assert "rate limit" in result["error"].lower()

@patch("server.requests.get")
def test_404_returns_readable_error(mock_get):
    mock_get.return_value = _mock_response(404)
    result = server.list_open_issues("nope/nope")
    assert "404" in result["error"]

def test_invalid_repo_name_rejected():
    result = server.list_open_issues("not-owner-slash-name")
    assert "Invalid repo name" in result["error"]

# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

@patch("server.requests.get")
def test_transient_503_is_retried_then_succeeds(mock_get, no_real_sleeping):
    mock_get.side_effect = [
        _mock_response(503),
        _mock_response(200, ONE_ISSUE),
    ]
    result = server.list_open_issues("owner/repo")
    assert result["count"] == 1
    assert mock_get.call_count == 2
    assert no_real_sleeping.call_count == 1


@patch("server.requests.get")
def test_persistent_503_gives_up_after_max_attempts(mock_get, no_real_sleeping):
    mock_get.return_value = _mock_response(503)
    result = server.list_open_issues("owner/repo")
    assert mock_get.call_count == server.MAX_ATTEMPTS
    assert "gave up after" in result["error"]
    # Sleeps happen between attempts, not after the last one.
    assert no_real_sleeping.call_count == server.MAX_ATTEMPTS - 1


@patch("server.requests.get")
def test_backoff_grows_exponentially(mock_get, no_real_sleeping):
    mock_get.return_value = _mock_response(503)
    server.list_open_issues("owner/repo")
    delays = [call.args[0] for call in no_real_sleeping.call_args_list]
    assert delays == sorted(delays)
    assert delays[1] >= delays[0] * 1.8  # doubling, allowing for jitter
    assert all(d <= server.RETRY_MAX_DELAY for d in delays)


@patch("server.requests.get")
def test_network_error_is_retried(mock_get, no_real_sleeping):
    mock_get.side_effect = [
        requests.ConnectionError("connection reset"),
        _mock_response(200, ONE_ISSUE),
    ]
    result = server.list_open_issues("owner/repo")
    assert result["count"] == 1
    assert mock_get.call_count == 2


@patch("server.requests.get")
def test_persistent_network_error_reports_readable_error(mock_get):
    mock_get.side_effect = requests.ConnectionError("connection reset")
    result = server.list_open_issues("owner/repo")
    assert "Network error" in result["error"]
    assert "gave up after" in result["error"]


@patch("server.requests.get")
def test_secondary_rate_limit_is_retried_honouring_retry_after(mock_get, no_real_sleeping):
    """A short abuse-detection limit should be waited out, not surfaced."""
    mock_get.side_effect = [
        _mock_response(
            403,
            {"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "3", "X-RateLimit-Remaining": "4999"},
        ),
        _mock_response(200, ONE_ISSUE),
    ]
    result = server.list_open_issues("owner/repo")
    assert result["count"] == 1
    no_real_sleeping.assert_called_once_with(3.0)


@patch("server.requests.get")
def test_secondary_rate_limit_without_retry_after_is_still_retried(mock_get, no_real_sleeping):
    mock_get.side_effect = [
        _mock_response(403, {"message": "abuse detection mechanism triggered"}),
        _mock_response(200, ONE_ISSUE),
    ]
    assert server.list_open_issues("owner/repo")["count"] == 1
    assert mock_get.call_count == 2


@patch("server.requests.get")
def test_primary_rate_limit_is_not_retried(mock_get, no_real_sleeping):
    """The hourly limit can be an hour out — fail fast instead of blocking."""
    mock_get.return_value = _mock_response(
        403, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}
    )
    result = server.list_open_issues("owner/repo")
    assert "rate limit" in result["error"].lower()
    assert mock_get.call_count == 1
    no_real_sleeping.assert_not_called()


@patch("server.requests.get")
def test_404_is_not_retried(mock_get, no_real_sleeping):
    mock_get.return_value = _mock_response(404)
    server.list_open_issues("nope/nope")
    assert mock_get.call_count == 1


@patch("server.requests.get")
def test_401_is_not_retried(mock_get, no_real_sleeping):
    mock_get.return_value = _mock_response(401)
    result = server.list_open_issues("owner/repo")
    assert "401" in result["error"]
    assert mock_get.call_count == 1

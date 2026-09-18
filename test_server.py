from unittest.mock import patch, MagicMock
import server

def _mock_response(status_code, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.text = str(json_data)
    return resp

@patch("server.requests.get")
def test_list_open_issues_success(mock_get):
    mock_get.return_value = _mock_response(200, [
        {"number": 1, "title": "Bug", "user": {"login": "alice"},
         "labels": [], "created_at": "2024-01-01T00:00:00Z", "html_url": "x"}
    ])
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
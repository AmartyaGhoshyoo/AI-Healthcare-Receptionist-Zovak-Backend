from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from zovak.api.routes import google_oauth_dev as oauth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/v1/auth/google/callback")
    app = FastAPI()
    app.include_router(oauth.router)
    with TestClient(app) as client:
        yield client


def test_start_sets_browser_bound_state_and_pkce(client):
    result = client.get("/api/v1/auth/google/start", follow_redirects=False)
    assert result.status_code == 302
    url = urlsplit(result.headers["location"])
    assert url.hostname == "accounts.google.com"
    params = parse_qs(url.query)
    assert params["access_type"] == ["offline"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [client.cookies.get(oauth.COOKIE_NAME)]
    assert len(params["code_challenge"][0]) > 30
    assert set(params["scope"][0].split()) == set(oauth.SCOPES)


def test_callback_rejects_invalid_state(client):
    start = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    invalid = client.get("/api/v1/auth/google/callback", params={"state": "incorrect", "code": "fake"})
    assert invalid.status_code == 400
    assert client.get("/api/v1/auth/google/status").json()["connected"] is False
    # Valid state still exists, but a subsequent success would require legitimate token exchange.
    assert state


def test_callback_exchanges_code_once_and_redacts_tokens(client, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"access_token": "PRIVATE_ACCESS", "refresh_token": "PRIVATE_REFRESH",
                    "expires_in": 3600, "scope": " ".join(oauth.SCOPES)}

    class FakeHTTPClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def __init__(self, **kwargs): pass
        async def post(self, url, data):
            assert url == oauth.TOKEN_URL
            assert data["code"] == "auth-code"
            assert len(data["code_verifier"]) > 40
            assert data["client_secret"] == "test-client-secret"
            return FakeResponse()

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeHTTPClient)
    start = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    response = client.get("/api/v1/auth/google/callback", params={"state": state, "code": "auth-code"})
    assert response.status_code == 200
    assert "PRIVATE" not in response.text
    assert client.get("/api/v1/auth/google/status").json() == {
        "connected": True, "refresh_token_available": True,
    }
    replay = client.get("/api/v1/auth/google/callback", params={"state": state, "code": "auth-code"})
    assert replay.status_code == 400


def test_callback_rejects_partial_scope(client, monkeypatch):
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"access_token": "foo", "scope": oauth.SCOPES[0]}
    class FakeHTTPClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, data): return FakeResponse()
    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeHTTPClient)
    start = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    response = client.get("/api/v1/auth/google/callback", params={"state": state, "code": "auth-code"})
    assert response.status_code == 403
    assert not client.get("/api/v1/auth/google/status").json()["connected"]

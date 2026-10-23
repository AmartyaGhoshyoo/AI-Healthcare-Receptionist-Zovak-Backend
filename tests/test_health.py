import httpx
import pytest
from zovak.main import create_app


@pytest.mark.asyncio
async def test_health(monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_ROUTES", "false")
    monkeypatch.setenv("APP_ENV", "development")
    from zovak.core.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert app.openapi()["paths"].get("/api/v1/dev/chat") is None
    get_settings.cache_clear()


def test_dev_routes_blocked_in_production(monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_ROUTES", "true")
    monkeypatch.setenv("APP_ENV", "production")
    from zovak.core.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Refusing"):
        create_app()
    get_settings.cache_clear()

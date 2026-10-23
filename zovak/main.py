from contextlib import asynccontextmanager
from fastapi import FastAPI
from zovak.api.routes.health import router as health_router
from zovak.core.config import get_settings
from zovak.services.mock_scheduler import DemoScheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    from zovak.services.google_scheduler import GoogleBackedScheduler
    app.state.scheduler = GoogleBackedScheduler(app)
    app.state.settings = get_settings()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.enable_dev_routes and settings.app_env != "development":
        raise RuntimeError("Refusing to expose unauthenticated development routes outside development")
    app = FastAPI(title="ZOVAK Backend", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)

    if settings.enable_dev_routes:
        from zovak.api.routes.google_oauth_dev import router as google_oauth_router
        app.include_router(google_oauth_router)
        from zovak.api.routes.dev import router as dev_router
        app.include_router(dev_router)

        from zovak.api.routes.voice_dev import (
            router as voice_router
        )
        app.include_router(voice_router)

    return app


app = create_app()

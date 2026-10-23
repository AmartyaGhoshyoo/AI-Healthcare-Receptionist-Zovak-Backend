from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zovak.core.config import get_settings


def make_session_factory():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    enable_dev_routes: bool = False
    openai_api_key: str = ""
    openai_agent_model: str = "gpt-6-luna"
    database_url: str = "postgresql+asyncpg://zovak:zovak@localhost:5432/zovak"


@lru_cache

def get_settings() -> Settings:
    return Settings()

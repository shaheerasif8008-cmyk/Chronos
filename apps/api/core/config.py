from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    org_id: str = "default"
    region: str = "us"
    admin_email: str = "admin@example.com"
    database_url: str = "postgresql+asyncpg://chronos:chronos@localhost:5432/chronos"
    redis_url: str = "redis://localhost:6379"
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "llama3"
    backup_api_key: str = ""
    jwt_secret: str = "change-me-in-dev"
    access_token_expire_minutes: int = 60

    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

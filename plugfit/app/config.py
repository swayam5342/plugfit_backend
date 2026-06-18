from functools import lru_cache
import secrets
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./plugfit_dev.db")

    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    JWT_SECRET: str = Field(default_factory=lambda: secrets.token_hex(64))

    JWT_ALGORITHM: str = Field(default="HS256")

    JWT_EXPIRE_MINUTES: int = Field(default=60 * 24 * 7)

    API_KEY_PREFIX: str = Field(default="pf_")

    MAX_SPEC_SIZE_BYTES: int = Field(default=5 * 1024 * 1024)

    MAX_TOOLS_PER_SPEC: int = Field(default=200)

    PIPELINE_TIMEOUT_SECONDS: int = Field(default=120)

    MCP_BASE_PATH: str = Field(default="/mcp")

    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "*"]
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

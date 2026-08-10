from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    postgres_dsn: str = "postgresql+asyncpg://cti_app:local-postgres-only@postgres:5432/cti_app"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint: str = "minio:9000"
    s3_access_key: str = "local-minio-user"
    s3_secret_key: str = "local-minio-password"
    s3_bucket: str = "cti-local"
    s3_secure: bool = False
    subject_workspace_root: Path = Path("work/subjects")
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    job_retry_base_seconds: float = Field(default=1.0, gt=0, le=3600)
    job_retry_max_seconds: float = Field(default=300.0, gt=0, le=86400)
    job_heartbeat_timeout_seconds: float = Field(default=120.0, gt=1, le=86400)
    job_recovery_interval_seconds: float = Field(default=30.0, gt=1, le=3600)
    openai_bridge_base_url: str = "http://127.0.0.1:8001/v1"
    openai_bridge_api_key: SecretStr | None = None
    openai_research_model: str = "chatgpt-web"
    openai_structured_model: str = "chatgpt-web"
    openai_drafting_model: str = "chatgpt-web"
    openai_critic_model: str = "chatgpt-web"
    qwen_base_url: str = "http://127.0.0.1:8080/v1"
    qwen_api_key: SecretStr | None = None
    qwen_model: str = "Qwen3-32B"
    qwen_is_external: bool = False
    model_force_adapter: Literal["auto", "openai", "qwen", "fake"] = "auto"
    model_request_timeout_seconds: float = Field(default=900.0, gt=0, le=3600)
    collection_max_redirects: int = Field(default=5, ge=0, le=10)
    collection_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    collection_max_download_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    collection_max_expanded_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    collection_max_decompression_ratio: float = Field(default=20.0, gt=1, le=1000)
    collection_allowed_domains: str = ""
    collection_blocked_domains: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    openai_bridge_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    openai_bridge_capabilities_timeout_seconds: float = Field(default=2.0, gt=0, le=2)
    openai_bridge_max_attempts: int = Field(default=3, ge=1, le=10)
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
    model_conversation_retention_days: int = Field(default=90, ge=1, le=3650)
    discovery_chatgpt_structuring_fallback: bool = False
    collection_max_redirects: int = Field(default=5, ge=0, le=10)
    collection_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    collection_max_download_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    collection_max_expanded_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    collection_max_decompression_ratio: float = Field(default=20.0, gt=1, le=1000)
    collection_allowed_domains: str = ""
    collection_blocked_domains: str = ""
    collection_fetch_lease_seconds: float = Field(default=120.0, gt=1, le=3600)
    pdf_max_document_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    pdf_max_pages: int = Field(default=200, gt=0, le=10_000)
    pdf_parse_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    pdf_max_text_chars: int = Field(default=2_000_000, gt=0)
    pdf_max_metadata_length: int = Field(default=16_384, gt=0)
    qwen_chunk_max_chars: int = Field(default=12_000, gt=100)
    qwen_chunk_overlap_chars: int = Field(default=500, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()

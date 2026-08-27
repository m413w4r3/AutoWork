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
    production_major_assisted_enabled: bool = False
    log_level: str = "INFO"
    postgres_dsn: str = "postgresql+asyncpg://cti_app:local-postgres-only@postgres:5432/cti_app"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint: str = "minio:9000"
    s3_access_key: str = "local-minio-user"
    s3_secret_key: str = "local-minio-password"
    s3_bucket: str = "cti-local"
    s3_secure: bool = False
    subject_workspace_root: Path = Path("work/subjects")
    # Local development trail of model answers and parser decisions.
    # Empty disables it; never enable outside a local environment.
    diagnostics_log_root: Path | None = None
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    job_retry_base_seconds: float = Field(default=1.0, gt=0, le=3600)
    job_retry_max_seconds: float = Field(default=300.0, gt=0, le=86400)
    job_heartbeat_timeout_seconds: float = Field(default=120.0, gt=1, le=86400)
    job_recovery_interval_seconds: float = Field(default=30.0, gt=1, le=3600)
    # Doit rester supérieur au BRIDGE_TOTAL_TIMEOUT du bridge : le worker attend
    # la fin de la génération, puis parse, persiste et regroupe.
    job_actor_time_limit_seconds: float = Field(default=4500.0, gt=600, le=86400)
    openai_bridge_base_url: str = "http://127.0.0.1:8001/v1"
    openai_bridge_api_key: SecretStr | None = None
    openai_bridge_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    openai_bridge_capabilities_timeout_seconds: float = Field(default=2.0, gt=0, le=2)
    openai_bridge_max_attempts: int = Field(default=3, ge=1, le=10)
    discovery_bridge_poll_interval_seconds: float = Field(default=5.0, ge=3.0, le=10.0)
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
    # VirusTotal is intentionally disabled unless a proxy and capabilities are
    # explicitly supplied by the composition root. `virustotal_api_key`, when
    # set, only makes the direct transport *available to be wired*; it never
    # authorizes an operation and never selects a route by itself. Which
    # transport an operation actually uses is a separate, explicit,
    # deny-by-default decision (see application.virustotal.VirusTotalRoutingPolicy
    # and the *_fallback_enabled flags below) — proxy stays the only transport
    # used unless a fallback is explicitly turned on.
    virustotal_proxy_url: str | None = None
    virustotal_file_report_enabled: bool = False
    virustotal_base_url: str = "http://www.virustotal.com/api/v3"
    virustotal_fallback_base_url: str | None = "http://www.virustotal.com/api/v3"
    virustotal_legacy_base_url: str | None = "http://www.virustotal.com/vtapi/v2"
    virustotal_api_key: SecretStr | None = None
    # Explicit opt-in for file_report's proxy fallback base URL (still proxy
    # transport, second v3 base) and its legacy v2 direct fallback. Both
    # default to disabled: neither the presence of `virustotal_fallback_base_url`
    # / `virustotal_legacy_base_url` nor of `virustotal_api_key` enables them.
    virustotal_file_report_proxy_fallback_enabled: bool = False
    virustotal_file_report_legacy_fallback_enabled: bool = False
    virustotal_proxy_insecure: bool = False
    virustotal_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    virustotal_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    virustotal_max_response_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    virustotal_default_page_size: int = Field(default=40, ge=1, le=100)
    virustotal_max_page_size: int = Field(default=100, ge=1, le=100)
    virustotal_max_pages: int = Field(default=10, ge=1, le=100)
    virustotal_max_results: int = Field(default=1000, ge=1, le=100_000)
    # File bytes are never fetched unless this is explicitly turned on; the
    # capability flag alone does not enable the download route (see
    # application.virustotal.VirusTotalRoutingPolicy).
    virustotal_file_download_enabled: bool = False
    virustotal_download_max_bytes: int = Field(
        default=200 * 1024 * 1024, gt=0, le=2 * 1024 * 1024 * 1024
    )
    virustotal_download_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    # Exact hostnames the signed download URL is allowed to target. Empty by
    # default: deny-by-default until an operator explicitly configures it.
    virustotal_download_allowed_hosts: str = ""

    # An investigation's loop budget is read exclusively from here — never
    # from a domain default — so the deployed limits are the sole source of
    # truth for how far an analyst investigation may run.
    investigation_max_cycles: int = Field(default=3, ge=1, le=100)
    investigation_max_pivot_runs: int = Field(default=0, ge=0)
    investigation_max_hits_acquired: int = Field(default=0, ge=0)
    investigation_max_new_samples: int = Field(default=0, ge=0)
    investigation_max_vt_read_units: int = Field(default=0, ge=0)
    analysis_string_min_length: int = Field(default=4, ge=1, le=1024)
    analysis_max_strings: int = Field(default=10_000, ge=1, le=1_000_000)
    analysis_max_sample_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    analysis_capa_rules_path: Path = Path("rules/capa")
    analysis_capa_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    analysis_capa_max_output_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    analysis_capa_max_memory_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    code_ngram_sizes: tuple[int, ...] = (4, 6, 8)
    code_ngram_max_per_sample: int = Field(default=100_000, gt=0)
    smda_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    smda_max_output_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    smda_max_memory_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()

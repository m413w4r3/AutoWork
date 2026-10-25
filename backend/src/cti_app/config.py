from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST = "vtsamples.commondatastorage.googleapis.com"
DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES = 8 * 1024**3
DOTNET_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES = 8 * 1024**3
DOTNET_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES = 32 * 1024**3
DOTNET_GC_HEAP_HARD_LIMIT = "0x10000000"
DOTNET_GC_CONCURRENT = "0"


def _normalize_download_host_list(value: str) -> frozenset[str]:
    hosts: set[str] = set()
    for raw_host in value.split(","):
        host = raw_host.strip().lower()
        if not host:
            continue
        labels = host.split(".")
        if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
            raise ValueError(
                "virustotal_download_allowed_hosts must contain exact hostnames only "
                "(without scheme, path, port, wildcard, or userinfo)"
            )
        hosts.add(host)
    return frozenset(hosts)


def _normalize_download_port_list(value: str) -> frozenset[int]:
    ports: set[int] = set()
    for raw_port in value.split(","):
        candidate = raw_port.strip()
        if not candidate:
            continue
        if not candidate.isascii() or not candidate.isdecimal():
            raise ValueError("virustotal_download_allowed_ports must contain numeric TCP ports")
        port = int(candidate)
        if not 1 <= port <= 65535:
            raise ValueError("virustotal_download_allowed_ports contains an invalid TCP port")
        ports.add(port)
    return frozenset(ports)


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    app_env: str = "development"
    api_bind_host: str = "127.0.0.1"
    operator_api_token: SecretStr | None = None
    operator_client_api_token: SecretStr | None = None
    log_level: str = "INFO"
    postgres_dsn: str = "postgresql+asyncpg://cti_app:local-postgres-only@postgres:5432/cti_app"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint: str = "minio:9000"
    s3_access_key: str = "local-minio-user"
    s3_secret_key: str = "local-minio-password"
    s3_bucket: str = "cti-local"
    s3_secure: bool = False
    subject_workspace_root: Path = Path("var/workspaces/subjects")
    edition_workspace_root: Path = Path("var/workspaces/editions")
    goodware_cache_root: Path = Path("/var/cache/autowork/goodware")
    # Ephemeral PDF inspection projections (rasters, block locations). The API and the
    # worker must share this directory for locations computed at render time to be
    # visible to the inspection routes; None falls back to the process temp directory.
    pdf_inspection_cache_root: Path | None = None
    # Local development trail of model answers and parser decisions.
    # Empty disables it; never enable outside a local environment.
    diagnostics_log_root: Path | None = None
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    job_retry_base_seconds: float = Field(default=1.0, gt=0, le=3600)
    job_retry_max_seconds: float = Field(default=300.0, gt=0, le=86400)
    job_bridge_ui_retry_base_seconds: float = Field(default=300.0, gt=0, le=3600)
    job_bridge_ui_retry_max_seconds: float = Field(default=1800.0, gt=0, le=86400)
    job_heartbeat_timeout_seconds: float = Field(default=120.0, gt=1, le=86400)
    job_recovery_interval_seconds: float = Field(default=30.0, gt=1, le=3600)
    # Doit rester supérieur au BRIDGE_TOTAL_TIMEOUT du bridge : le worker attend
    # la fin de la génération, puis parse, persiste et regroupe.
    job_actor_time_limit_seconds: float = Field(default=4500.0, gt=600, le=86400)
    production_subject_jitter_min_seconds: float = Field(default=30.0, ge=0)
    production_subject_jitter_max_seconds: float = Field(default=90.0, ge=0)
    production_model_jitter_min_seconds: float = Field(default=8.0, ge=0)
    production_model_jitter_max_seconds: float = Field(default=20.0, ge=0)
    production_relevance_classifier_enabled: bool = True
    # External web search from Editorial Enrichment is an explicit operator opt-in.
    production_editorial_resource_search_enabled: bool = False
    # Palier de repos long inséré périodiquement pour laisser le bridge
    # ChatGPT récupérer. Zéro désactive le palier.
    production_cooldown_every_n_subjects: int = Field(default=3, ge=0)
    production_cooldown_min_seconds: float = Field(default=600.0, ge=0)
    production_cooldown_max_seconds: float = Field(default=1200.0, ge=0)
    openai_bridge_base_url: str = "http://127.0.0.1:8001/v1"
    openai_bridge_api_key: SecretStr | None = None
    openai_bridge_min_submission_interval_seconds: float = Field(default=20.0, ge=0, le=3600)
    openai_bridge_submission_jitter_fraction: float = Field(default=0.3, ge=0, le=0.3)
    operator_advisor_checkpoint_tool_threshold: int = Field(default=12, ge=1, le=1000)
    model_output_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=5 * 1024 * 1024 * 1024)
    # Bound in-memory verified sample reads before Base64 prompt encoding.
    advisor_sample_blob_max_bytes: int = Field(
        default=64 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024
    )
    openai_bridge_pre_submission_retry_base_seconds: float = Field(default=1.0, ge=0, le=300)
    openai_bridge_pre_submission_retry_max_seconds: float = Field(default=8.0, ge=0, le=3600)
    openai_bridge_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    openai_bridge_capabilities_timeout_seconds: float = Field(default=2.0, gt=0, le=2)
    # Legacy/default synchronous request budget. Research has a separate longer
    # role budget below; the old OPENAI_BRIDGE_WAIT_TIMEOUT_SECONDS name remains valid.
    openai_bridge_wait_timeout_seconds: float = Field(default=3600.0, gt=0, le=86400)
    openai_bridge_wait_timeout_research_seconds: float = Field(default=7200.0, gt=0, le=86400)
    # Fermer un onglet exige un aller-retour WebSocket vers l'extension Chrome.
    # Le budget doit couvrir BRIDGE_UI_TIMEOUT (30 s) plus la fenêtre de
    # reconnexion BRIDGE_RECONNECT_GRACE (20 s) du service worker MV3.
    openai_bridge_archive_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    discovery_bridge_poll_interval_seconds: float = Field(default=5.0, ge=3.0, le=10.0)
    openai_research_model: str = "chatgpt-web"
    openai_structured_model: str = "chatgpt-web"
    openai_drafting_model: str = "chatgpt-web"
    openai_critic_model: str = "chatgpt-web"
    qwen_base_url: str = "http://127.0.0.1:8080/v1"
    qwen_api_key: SecretStr | None = None
    qwen_model: str = "Qwen3-32B"
    qwen_is_external: bool = False
    webai_base_url: str = "http://web_ai:6969/v1"
    webai_api_key: SecretStr | None = None
    webai_model: str = "gemini-3-flash"
    webai_is_external: bool = True
    model_force_adapter: Literal[
        "auto", "chatgpt_bridge", "gemini_webai", "qwen", "fake", "openai", "gemini"
    ] = "auto"
    model_route_web_research: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_route_bulk_extraction: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_route_ambiguous_clustering: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_route_standard_draft: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_route_premium_synthesis: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    # Gemini WebAI n'expose pas encore de contrat structured : il est exclu tant
    # que l'enrichissement éditorial exige une sortie structurée.
    model_route_editorial_enrichment: Literal["chatgpt_bridge", "qwen", "fake"] = "chatgpt_bridge"
    model_route_critique: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_route_discovery_merge: Literal["chatgpt_bridge", "gemini_webai", "qwen", "fake"] = (
        "chatgpt_bridge"
    )
    model_request_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    model_request_timeout_research_seconds: float = Field(default=900.0, gt=0, le=3600)
    # This legacy setting is now the background safety ceiling, not a fixed
    # deadline that overrides bridge progress.
    model_background_wait_timeout_seconds: float = Field(default=5400.0, gt=0, le=86400)
    model_background_wait_timeout_research_seconds: float | None = Field(
        default=None, ge=5400, le=86400
    )
    model_background_idle_timeout_seconds: float = Field(default=1200.0, gt=0, le=86400)
    model_background_idle_timeout_research_seconds: float | None = Field(
        default=None, gt=0, le=86400
    )
    code_version: str = "0.1.0"
    # OSINT_OPEN is opt-in. These are generous policy defaults; independent hard
    # maxima bound any activation or extension request.
    osint_open_default_vt_units: int = Field(default=10_000, ge=1)
    osint_open_default_downloads: int = Field(default=1_000, ge=1)
    osint_open_default_bridge_turns: int = Field(default=100, ge=1)
    osint_open_default_tool_seconds: int = Field(default=3_600, ge=1)
    osint_open_default_context_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    osint_open_default_context_tokens: int = Field(default=4_000_000, ge=1)
    osint_open_default_shodan_credits: int | None = Field(default=None, ge=1)
    osint_open_default_acquire_hits_allowed: bool = True
    osint_open_default_max_effect_claims: int = Field(default=500, ge=1)
    osint_open_max_vt_units: int = Field(default=100_000, ge=1)
    osint_open_max_downloads: int = Field(default=10_000, ge=1)
    osint_open_max_bridge_turns: int = Field(default=1_000, ge=1)
    osint_open_max_tool_seconds: int = Field(default=86_400, ge=1)
    osint_open_max_context_bytes: int = Field(default=128 * 1024 * 1024, ge=1)
    osint_open_max_context_tokens: int = Field(default=32_000_000, ge=1)
    osint_open_max_shodan_credits: int = Field(default=100_000, ge=1)
    osint_open_max_effect_claims: int = Field(default=10_000, ge=1)
    worker_code_version: str | None = None
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
    # The configured proxy appends its own apikey with a raw '&'. Keep at least
    # one harmless query parameter on otherwise queryless proxy-v3 requests.
    virustotal_proxy_query_sentinel_enabled: bool = True
    # Retrohunt is an explicit opt-in and uses the same VirusTotal API v3 base,
    # proxy transport, and API key as ordinary VirusTotal requests.
    virustotal_retrohunt_enabled: bool = False
    virustotal_retrohunt_max_result_pages: int = Field(default=20, ge=1, le=100)
    virustotal_retrohunt_max_results: int = Field(default=1000, ge=1, le=100_000)
    virustotal_file_report_enabled: bool = False
    virustotal_object_relationships_enabled: bool = False
    virustotal_intelligence_search_enabled: bool = False
    virustotal_behaviour_reports_enabled: bool = False
    virustotal_base_url: str = "http://www.virustotal.com/api/v3"
    virustotal_fallback_base_url: str | None = "http://www.virustotal.com/api/v3"
    virustotal_legacy_base_url: str | None = "https://www.virustotal.com/vtapi/v2"
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
    virustotal_file_download_legacy_fallback_enabled: bool = False
    virustotal_behaviour_pcap_enabled: bool = False
    virustotal_download_max_bytes: int = Field(
        default=200 * 1024 * 1024, gt=0, le=2 * 1024 * 1024 * 1024
    )
    virustotal_download_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    virustotal_download_max_redirects: int = Field(default=3, ge=0, le=10)
    # Signed downloads currently use this exact VirusTotal CDN hostname.
    virustotal_download_allowed_hosts: str = VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST
    # TCP ports are separately allowlisted; HTTPS's conventional port is the
    # only default. Redirects on a nonstandard port require an explicit opt-in.
    virustotal_download_allowed_ports: str = "443"

    @field_validator("virustotal_download_allowed_hosts")
    @classmethod
    def validate_download_allowed_hosts(cls, value: str) -> str:
        return ",".join(sorted(_normalize_download_host_list(value)))

    @field_validator("virustotal_download_allowed_ports")
    @classmethod
    def validate_download_allowed_ports(cls, value: str) -> str:
        return ",".join(str(port) for port in sorted(_normalize_download_port_list(value)))

    @property
    def virustotal_retrohunt_proxy_enabled(self) -> bool:
        """Compatibility for the API/worker client lifecycle until their next lot."""
        return self.virustotal_retrohunt_enabled

    @property
    def virustotal_retrohunt_proxy_base_url(self) -> str:
        """Compatibility sentinel; Retrohunt routes use virustotal_base_url."""
        return self.virustotal_base_url

    @property
    def virustotal_retrohunt_proxy_allowed_hosts(self) -> str:
        """Compatibility sentinel; endpoint trust is validated by the v3 client."""
        return "www.virustotal.com"

    # Shodan exposes only passive, indexed observations through this port.
    # Each capability and the API key are opt-in; their presence never
    # authorizes active scans or selects any write endpoint.
    shodan_host_enabled: bool = False
    shodan_search_enabled: bool = False
    shodan_domain_enabled: bool = False
    shodan_count_enabled: bool = False
    shodan_base_url: str = "https://api.shodan.io"
    shodan_api_key: SecretStr | None = None
    shodan_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    shodan_read_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    shodan_max_response_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    shodan_max_pages: int = Field(default=10, ge=1, le=100)
    shodan_max_results: int = Field(default=1000, ge=1, le=100_000)

    # Local analysis is an explicit opt-in. The runner keeps a typed command
    # catalogue, a minimal environment, resource limits, and network syscall
    # filtering; these settings only widen its bounded defaults.
    local_analysis_enabled: bool = False
    local_analysis_require_filesystem_confinement: bool = True
    local_analysis_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    local_analysis_max_output_bytes: int = Field(default=1_000_000, gt=0, le=20_000_000)
    local_analysis_cpu_limit_seconds: int = Field(default=30, ge=1, le=600)
    local_analysis_memory_limit_bytes: int = Field(
        default=512 * 1024 * 1024, ge=64 * 1024 * 1024, le=4 * 1024 * 1024 * 1024
    )
    # CoreCLR reserves virtual address space; this RLIMIT_AS ceiling is not an RSS bound.
    # The analysis-worker Compose cgroup remains the effective aggregate memory limit.
    dotnet_analysis_address_space_limit_bytes: int = Field(
        default=DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES,
        ge=DOTNET_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES,
        le=DOTNET_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES,
    )
    local_analysis_file_size_limit_bytes: int = Field(
        default=1 * 1024 * 1024, ge=4096, le=100 * 1024 * 1024
    )
    local_analysis_max_sample_bytes: int = Field(
        default=200 * 1024 * 1024, gt=0, le=2 * 1024 * 1024 * 1024
    )
    # The confined worker is a separate, non-root Compose service. It is probed
    # before its families become visible; the profile and version pins are not
    # secrets and must match the worker image at runtime.
    analysis_worker_enabled: bool = False
    analysis_worker_probe_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    analysis_worker_job_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    analysis_worker_poll_interval_seconds: float = Field(default=0.25, gt=0, le=5)
    analysis_worker_result_max_bytes: int = Field(default=1_000_000, gt=0, le=20_000_000)
    analysis_worker_expected_isolation_profile: str = Field(
        default="bookworm-internal-v1", min_length=1, max_length=128
    )
    analysis_local_file_expected_version: str = Field(default="5.44", min_length=1, max_length=64)
    analysis_local_binutils_expected_version: str = Field(
        default="2.40", min_length=1, max_length=64
    )
    analysis_local_xxd_expected_version: str = Field(
        default="2022-01-14", min_length=1, max_length=64
    )
    analysis_local_coreutils_expected_version: str = Field(
        default="9.1", min_length=1, max_length=64
    )
    analysis_local_command_max_range_bytes: int = Field(default=1_048_576, gt=0, le=1_048_576)
    analysis_suricata_expected_version: str = Field(default="7.0.10", min_length=1, max_length=64)
    analysis_suricata_rules_path: Path = Path("/app/rules/suricata")
    analysis_suricata_ruleset_revision: str = Field(
        default="autowork-suricata-v1", min_length=1, max_length=128
    )
    analysis_suricata_ruleset_sha256: str = Field(
        default="66f69f0cf586a87f739d4f63c6bb9d23a7d303f8adf23c2773a31839ea7f9f59",
        pattern=r"^[0-9a-f]{64}$",
    )
    analysis_suricata_timeout_seconds: float = Field(default=60.0, gt=0, le=120)
    analysis_suricata_max_eve_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )
    analysis_suricata_max_events: int = Field(default=24, gt=0, le=100)

    # MWCP runs only parsers listed here (parser id -> pinned parser version);
    # an empty allowlist keeps configuration extraction unavailable.
    mwcp_parser_versions: dict[str, str] = Field(default_factory=dict)

    # Ghidra runs as the optional isolated headless service. The client admits
    # it only after the fixed service contract and both runtime versions attest.
    ghidra_enabled: bool = False
    ghidra_base_url: str = "http://ghidra:8787"
    ghidra_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    ghidra_read_timeout_seconds: float = Field(default=150.0, gt=0, le=600)
    ghidra_max_sample_bytes: int = Field(default=200 * 1024 * 1024, gt=0, le=2 * 1024 * 1024 * 1024)
    ghidra_max_response_bytes: int = Field(default=2 * 1024 * 1024, gt=0, le=20 * 1024 * 1024)
    ghidra_expected_version: str = Field(default="12.1.4", min_length=1, max_length=64)
    ghidra_expected_java_version: str = Field(default="21.0.12.1", min_length=1, max_length=64)

    # Native inspection is opt-in and advertises one closed, read-only catalog.
    # The REA worker client is supplied by the deployment and must independently
    # report its confinement guarantees before the adapter becomes available.
    native_analysis_enabled: bool = False
    native_analysis_provider: Literal["rea", "ghidra_mcp", "none"] = "none"
    native_analysis_catalog_version: str = Field(
        default="autowork-native-read.v1", min_length=1, max_length=128
    )
    native_analysis_rea_expected_version: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    native_analysis_timeout_seconds: float = Field(default=150.0, gt=0, le=600)
    native_analysis_max_response_bytes: int = Field(default=64 * 1024, ge=256, le=2 * 1024 * 1024)
    native_analysis_max_output_bytes: int = Field(default=32 * 1024, ge=256, le=64 * 1024)

    # Internal autonomous investigation cycles only. This does not cap
    # operator-led Bridge consultations or the human-approved G2 bridge_turns.
    investigation_max_cycles: int = Field(default=12, ge=1, le=100)
    investigation_max_pivot_runs: int = Field(default=0, ge=0)
    investigation_max_hits_acquired: int = Field(default=0, ge=0)
    investigation_max_new_samples: int = Field(default=0, ge=0)
    investigation_max_vt_read_units: int = Field(default=0, ge=0)
    investigation_max_shodan_credits: int = Field(default=0, ge=0)
    investigation_max_tool_seconds: int = Field(default=3600, ge=1)
    investigation_max_context_bytes: int = Field(default=4_000_000, ge=1)
    investigation_no_progress_limit: int = Field(default=3, ge=1, le=20)
    osint_open_vt_units: int = Field(default=10_000, ge=0)
    osint_open_downloads: int = Field(default=1_000, ge=0)
    osint_open_bridge_turns: int = Field(default=100, ge=0)
    osint_open_tool_seconds: int = Field(default=3_600, ge=1)
    osint_open_context_bytes: int = Field(default=16_000_000, ge=1)
    osint_open_context_tokens: int = Field(default=4_000_000, ge=1)
    osint_open_shodan_credits: int | None = Field(default=None, ge=0)
    osint_open_default_tlp: Literal["white", "clear", "green", "amber", "red"] = "clear"
    osint_open_acquire_hits_allowed: bool = True
    osint_open_max_effect_claims_per_investigation: int | None = Field(default=500, ge=1)
    rule_workbench_enabled: bool = False
    analysis_string_min_length: int = Field(default=4, ge=1, le=1024)
    analysis_max_strings: int = Field(default=10_000, ge=1, le=1_000_000)
    analysis_max_sample_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    analysis_capa_rules_path: Path = Path("rules/capa")
    analysis_capa_expected_version: str = Field(default="9.4.0", min_length=1, max_length=64)
    analysis_smda_expected_version: str = Field(default="4.5.0", min_length=1, max_length=64)
    analysis_capa_ruleset_revision: str = Field(
        default="2af9fbfc1c9b4634dbeb76b5d34fca9389fa7f80", min_length=7, max_length=64
    )
    analysis_tshark_expected_version: str = Field(default="4.0.17", min_length=1, max_length=64)
    analysis_capinfos_expected_version: str = Field(default="4.0.17", min_length=1, max_length=64)
    analysis_capa_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    analysis_capa_max_output_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    analysis_capa_max_memory_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    analysis_yara_timeout_seconds: float = Field(default=30.0, gt=0, le=30)
    analysis_yara_max_strings_per_rule: int = Field(default=10_000, gt=0, le=10_000)
    analysis_floss_max_output_bytes: int = Field(
        default=12 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )
    analysis_local_monodis_expected_version: str = Field(
        default="6.8.0.105+dfsg-3.3+deb12u1", min_length=1, max_length=128
    )
    analysis_local_monodis_max_sample_bytes: int = Field(
        default=64 * 1024 * 1024, gt=0, le=200 * 1024 * 1024
    )
    analysis_local_monodis_max_output_bytes: int = Field(
        default=8 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    analysis_local_monodis_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    goodware_suspicious_count: int = Field(default=3, ge=1)
    goodware_banal_count: int = Field(default=10, ge=1)
    invariant_max_pattern_chars: int = Field(default=96, ge=1)
    code_ngram_sizes: tuple[int, ...] = (4, 6, 8)
    code_ngram_max_per_sample: int = Field(default=100_000, gt=0)
    code_ngram_max_mask_ratio: float = Field(default=0.20, ge=0, le=1)
    code_ngram_min_contiguous_fixed_bytes: int = Field(default=6, ge=0)
    likely_packed_operator: Literal["ALL", "ANY"] = "ALL"
    likely_packed_max_executable_section_entropy_gte: float = Field(default=7.2, ge=0)
    likely_packed_executable_bytes_per_function_gte: int = Field(default=1200, ge=0)
    likely_packed_known_packer_marker_hit: bool = True
    smda_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    smda_max_output_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    smda_max_memory_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    rule_corpus_import_max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)

    @model_validator(mode="after")
    def validate_production_jitter_bounds(self) -> Settings:
        if self.operator_api_token is not None:
            operator_token = self.operator_api_token.get_secret_value()
            if (
                len(operator_token) < 32
                or not operator_token.isascii()
                or any(character.isspace() for character in operator_token)
            ):
                raise ValueError(
                    "OPERATOR_API_TOKEN must contain at least 32 ASCII non-whitespace characters"
                )
        if self.operator_client_api_token is not None:
            client_token = self.operator_client_api_token.get_secret_value()
            if (
                len(client_token) < 32
                or not client_token.isascii()
                or any(character.isspace() for character in client_token)
            ):
                raise ValueError(
                    "OPERATOR_CLIENT_API_TOKEN must contain at least 32 ASCII "
                    "non-whitespace characters"
                )
            if (
                self.operator_api_token is not None
                and client_token == self.operator_api_token.get_secret_value()
            ):
                raise ValueError("OPERATOR_CLIENT_API_TOKEN must differ from OPERATOR_API_TOKEN")
        if self.production_subject_jitter_max_seconds < self.production_subject_jitter_min_seconds:
            raise ValueError("production subject jitter max must be >= min")
        if self.production_model_jitter_max_seconds < self.production_model_jitter_min_seconds:
            raise ValueError("production model jitter max must be >= min")
        if self.production_cooldown_max_seconds < self.production_cooldown_min_seconds:
            raise ValueError("production cooldown max must be >= min")
        if self.job_bridge_ui_retry_max_seconds < self.job_bridge_ui_retry_base_seconds:
            raise ValueError("bridge UI retry max must be >= base")
        if (
            self.openai_bridge_pre_submission_retry_max_seconds
            < self.openai_bridge_pre_submission_retry_base_seconds
        ):
            raise ValueError("Bridge pre-submission retry max must be >= base")
        # Preserve deployments using the former short total deadline, while
        # preventing it from cutting off active background work early.
        self.model_background_wait_timeout_seconds = max(
            5400.0, self.model_background_wait_timeout_seconds
        )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

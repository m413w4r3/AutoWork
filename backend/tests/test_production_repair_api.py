from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cti_app.api import main as api_main
from cti_app.api import production as production_api
from cti_app.api import publication as publication_api
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.config import Settings


class _Engine:
    async def dispose(self) -> None:
        return None


class _Readiness:
    async def close(self) -> None:
        return None


class _Goodware:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def check_startup(self) -> None:
        return None


class _CumulativeDiscovery:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.replan_intake = None

    def set_replan_intake(self, callback: object) -> None:
        self.replan_intake = callback


class _ProductionChain:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.mark.asyncio
async def test_real_app_lifespan_and_repair_fallbacks_require_revision_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="development",
        api_bind_host="127.0.0.1",
        virustotal_file_report_enabled=False,
        virustotal_object_relationships_enabled=False,
        virustotal_intelligence_search_enabled=False,
        virustotal_file_download_enabled=False,
        virustotal_behaviour_reports_enabled=False,
        virustotal_retrohunt_proxy_enabled=False,
        shodan_host_enabled=False,
        shodan_search_enabled=False,
        shodan_domain_enabled=False,
        shodan_count_enabled=False,
        ghidra_enabled=False,
    )
    monkeypatch.setattr(api_main, "settings", settings)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_main, "InfrastructureReadinessChecker", lambda _settings: _Readiness())
    monkeypatch.setattr(
        api_main.ProductionPacingPolicy, "from_settings", lambda _settings: object()
    )
    monkeypatch.setattr(api_main, "create_postgres_engine", lambda _dsn: _Engine())
    monkeypatch.setattr(api_main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(api_main, "create_model_gateway", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_main, "Minio", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_main, "MinioBlobStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        api_main, "create_external_effect_coordinator", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        api_main, "create_bridge_capabilities_provider", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        api_main, "create_subject_collection_service", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(api_main, "GoodwareService", _Goodware)
    monkeypatch.setattr(api_main, "CumulativeDiscoveryService", _CumulativeDiscovery)
    monkeypatch.setattr(api_main, "ProductionStageChain", _ProductionChain)
    monkeypatch.setattr(
        api_main,
        "build_investigation_runtime_composition_factory",
        lambda *_args, **_kwargs: SimpleNamespace(composition_factory=object()),
    )
    monkeypatch.setattr(api_main, "make_investigation_runner", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_main, "create_job_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(api_main, "DramatiqJobDispatcher", lambda: object())
    monkeypatch.setattr(api_main, "describe_tool_inventory", lambda _composition: {})
    monkeypatch.setattr(api_main, "typst_bundle_paths", lambda: (Path("/tmp"),) * 3)
    monkeypatch.setattr(
        api_main.DiagnosticsLog,
        "from_env",
        lambda _root: DiagnosticsLog(None),
    )

    app = api_main.create_app()
    async with app.router.lifespan_context(app):
        request = SimpleNamespace(app=app)
        configured = app.state.production_repair_materialization_service
        assert configured._revision_effects is not None
        assert production_api._production_repair_materialization_service(request) is configured

        app.state.production_repair_materialization_service = None
        production_fallback = production_api._production_repair_materialization_service(request)
        assert production_fallback._revision_effects is not None

        app.state.production_repair_materialization_service = None
        publication_fallback = publication_api._repair_materialization_service(request)
        assert publication_fallback._revision_effects is not None


def test_production_repair_factory_rejects_missing_artifact_store() -> None:
    from cti_app.application.production_repair_factory import (
        create_production_repair_materialization_service,
    )

    with pytest.raises(RuntimeError, match="production_repair_revision_effects_unavailable"):
        create_production_repair_materialization_service(lambda: None, artifact_store=None)  # type: ignore[arg-type]

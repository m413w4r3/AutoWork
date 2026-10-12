from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production_control import router


class _MissingControlViewService:
    async def edition_page(self, edition_id: UUID, *, cursor: str | None, limit: int) -> object:
        del edition_id, cursor, limit
        raise LookupError("edition_not_found")

    async def run_view(self, run_id: UUID) -> object:
        del run_id
        raise LookupError("run_not_found")


@pytest.mark.asyncio
async def test_control_view_routes_validate_pagination_and_map_missing_resources() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.production_control_view_service = _MissingControlViewService()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid_limit = await client.get(
            f"/api/editions/{UUID(int=1)}/production/control-view?limit=26"
        )
        missing_edition = await client.get(f"/api/editions/{UUID(int=1)}/production/control-view")
        missing_run = await client.get(f"/api/production/runs/{UUID(int=2)}/control-view")

    assert invalid_limit.status_code == 422
    assert missing_edition.status_code == 404
    assert missing_edition.json()["detail"] == {"code": "edition_not_found"}
    assert missing_run.status_code == 404
    assert missing_run.json()["detail"] == {"code": "run_not_found"}

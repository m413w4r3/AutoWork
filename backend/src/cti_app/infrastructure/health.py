import asyncio
from collections.abc import Awaitable, Callable

from minio import Minio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cti_app.application.health import DependencyStatus
from cti_app.config import Settings

AsyncProbe = Callable[[], Awaitable[None]]


class InfrastructureReadinessChecker:
    """Read-only probes for the three local infrastructure dependencies."""

    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.readiness_timeout_seconds
        self._engine: AsyncEngine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
        self._redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self._minio = Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        )
        self._bucket = settings.s3_bucket

    async def check(self) -> dict[str, DependencyStatus]:
        names = ("postgresql", "redis", "object_storage")
        results = await asyncio.gather(
            self._run_probe(self._check_postgresql),
            self._run_probe(self._check_redis),
            self._run_probe(self._check_object_storage),
        )
        return dict(zip(names, results, strict=True))

    async def close(self) -> None:
        await self._engine.dispose()
        await self._redis.aclose()

    async def _run_probe(self, probe: AsyncProbe) -> DependencyStatus:
        try:
            await asyncio.wait_for(probe(), timeout=self._timeout)
        except Exception as exc:
            return DependencyStatus(status="unavailable", detail=type(exc).__name__)
        return DependencyStatus(status="ok")

    async def _check_postgresql(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _check_redis(self) -> None:
        if not await self._redis.ping():
            raise ConnectionError("Redis ping returned false")

    async def _check_object_storage(self) -> None:
        exists = await asyncio.to_thread(self._minio.bucket_exists, self._bucket)
        if not exists:
            raise RuntimeError("Configured object storage bucket does not exist")

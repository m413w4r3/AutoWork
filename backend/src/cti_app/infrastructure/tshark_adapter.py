"""Confined, bounded TShark/capinfos adapter for passive captures."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import cast

from cti_app.application.local_analysis import LocalCommandCapability, LocalCommandCatalog
from cti_app.domain.local_analysis import (
    LocalAnalysisFileType,
    LocalCommandId,
    LocalCommandOptionSpec,
    LocalCommandOutputKind,
    LocalCommandSpec,
    LocalCommandStatus,
    LocalOptionKind,
)
from cti_app.domain.network_protocol import NetworkCommandId, NetworkExtractionLimitsV1
from cti_app.infrastructure.local_command_runner import LocalCommandRunner

TSHARK_FIELDS = (
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "_ws.col.Protocol",
    "frame.protocols",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
    "udp.srcport",
    "udp.dstport",
    "tcp.stream",
    "udp.stream",
    "dns.qry.name",
    "dns.flags.response",
    "http.host",
    "http.request.method",
    "http.request.uri",
    "tls.handshake.extensions_server_name",
)
JA3_FIELD = "tls.handshake.ja3"


@dataclass(frozen=True, slots=True)
class TsharkPacketFields:
    frame_number: int
    timestamp: str | None
    frame_length: int | None
    protocol: str
    protocol_stack: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TsharkInspection:
    status: LocalCommandStatus
    detail_code: str | None
    tshark_version: str | None
    capinfos_version: str | None
    packets: tuple[TsharkPacketFields, ...]
    capinfos_summary: str
    output_truncated: bool
    ja3_available: bool | None
    limitations: tuple[str, ...] = ()


def network_local_command_specs(
    limits: NetworkExtractionLimitsV1 | None = None,
) -> tuple[LocalCommandSpec, ...]:
    configured = limits or NetworkExtractionLimitsV1()
    any_file = frozenset(LocalAnalysisFileType)
    return (
        LocalCommandSpec(
            id=cast(LocalCommandId, NetworkCommandId.TSHARK),
            binary="tshark",
            allowed_options=(
                LocalCommandOptionSpec("field_catalog", LocalOptionKind.BOOLEAN, False),
            ),
            accepted_file_types=any_file,
            timeout_seconds=25,
            max_output_bytes=configured.max_output_bytes,
            cpu_limit_seconds=20,
            memory_limit_bytes=512 * 1024 * 1024,
            file_size_limit_bytes=1 * 1024 * 1024,
            output_kind=LocalCommandOutputKind.TEXT,
            requires_filesystem_confinement=True,
        ),
        LocalCommandSpec(
            id=cast(LocalCommandId, NetworkCommandId.CAPINFOS),
            binary="capinfos",
            allowed_options=(),
            accepted_file_types=any_file,
            timeout_seconds=10,
            max_output_bytes=min(configured.max_output_bytes, 64 * 1024),
            cpu_limit_seconds=5,
            memory_limit_bytes=256 * 1024 * 1024,
            file_size_limit_bytes=1 * 1024 * 1024,
            output_kind=LocalCommandOutputKind.TEXT,
            requires_filesystem_confinement=True,
        ),
    )


class TsharkAdapter:
    def __init__(
        self,
        *,
        runner: LocalCommandRunner | None = None,
        catalog: LocalCommandCatalog | None = None,
        limits: NetworkExtractionLimitsV1 | None = None,
    ) -> None:
        self.limits = limits or NetworkExtractionLimitsV1()
        self.catalog = catalog or LocalCommandCatalog(
            specs=network_local_command_specs(self.limits),
            timeout_seconds=25,
            max_output_bytes=self.limits.max_output_bytes,
            cpu_limit_seconds=20,
            memory_limit_bytes=512 * 1024 * 1024,
        )
        self.runner = runner or LocalCommandRunner(max_sample_bytes=self.limits.max_capture_bytes)
        self._ja3_available: bool | None = None

    @property
    def available(self) -> bool:
        tshark = self._capability(NetworkCommandId.TSHARK)
        capinfos = self._capability(NetworkCommandId.CAPINFOS)
        return bool(
            self._filesystem_confined
            and tshark
            and tshark.available
            and capinfos
            and capinfos.available
        )

    def versions(self) -> tuple[str | None, str | None]:
        tshark = self._capability(NetworkCommandId.TSHARK)
        capinfos = self._capability(NetworkCommandId.CAPINFOS)
        return (
            tshark.version if tshark else None,
            capinfos.version if capinfos else None,
        )

    async def inspect(self, capture_bytes: bytes, sha256: str) -> TsharkInspection:
        tshark = self._capability(NetworkCommandId.TSHARK)
        capinfos = self._capability(NetworkCommandId.CAPINFOS)
        if not tshark or not capinfos or not tshark.available or not capinfos.available:
            return TsharkInspection(
                status=LocalCommandStatus.UNAVAILABLE,
                detail_code="binary_unavailable",
                tshark_version=tshark.version if tshark else None,
                capinfos_version=capinfos.version if capinfos else None,
                packets=(),
                capinfos_summary="",
                output_truncated=False,
                ja3_available=None,
                limitations=("TShark et capinfos sont des capacités locales optionnelles.",),
            )
        if not self._filesystem_confined:
            return TsharkInspection(
                status=LocalCommandStatus.UNAVAILABLE,
                detail_code="filesystem_confinement_required",
                tshark_version=tshark.version,
                capinfos_version=capinfos.version,
                packets=(),
                capinfos_summary="",
                output_truncated=False,
                ja3_available=None,
                limitations=(
                    "TShark et capinfos requièrent le confinement du système de fichiers.",
                ),
            )

        if self._ja3_available is None:
            catalog_result = await self.runner.run(
                tshark,
                sample_sha256=sha256,
                sample_bytes=capture_bytes,
                options={"field_catalog": True},
            )
            ja3_found = any(
                line.rstrip().endswith(f"\t{JA3_FIELD}")
                for line in catalog_result.output.splitlines()
            )
            if catalog_result.status is LocalCommandStatus.SUCCEEDED:
                self._ja3_available = ja3_found
            elif catalog_result.status is LocalCommandStatus.OUTPUT_TRUNCATED and ja3_found:
                self._ja3_available = True

        tshark_result = await self.runner.run(
            tshark,
            sample_sha256=sha256,
            sample_bytes=capture_bytes,
            options={"include_ja3": self._ja3_available is True},
        )
        if tshark_result.status not in {
            LocalCommandStatus.SUCCEEDED,
            LocalCommandStatus.EMPTY,
            LocalCommandStatus.OUTPUT_TRUNCATED,
        }:
            return TsharkInspection(
                status=tshark_result.status,
                detail_code=tshark_result.detail_code,
                tshark_version=tshark.version,
                capinfos_version=capinfos.version,
                packets=(),
                capinfos_summary="",
                output_truncated=tshark_result.status is LocalCommandStatus.OUTPUT_TRUNCATED,
                ja3_available=self._ja3_available,
                limitations=("TShark n'a pas produit de champs de paquets exploitables.",),
            )

        capinfos_result = await self.runner.run(
            capinfos,
            sample_sha256=sha256,
            sample_bytes=capture_bytes,
            options={},
        )
        if capinfos_result.status not in {
            LocalCommandStatus.SUCCEEDED,
            LocalCommandStatus.EMPTY,
            LocalCommandStatus.OUTPUT_TRUNCATED,
        }:
            return TsharkInspection(
                status=capinfos_result.status,
                detail_code=capinfos_result.detail_code,
                tshark_version=tshark.version,
                capinfos_version=capinfos.version,
                packets=(),
                capinfos_summary="",
                output_truncated=False,
                ja3_available=self._ja3_available,
                limitations=("capinfos n'a pas confirmé les métadonnées de capture.",),
            )

        include_ja3 = self._ja3_available is True
        packets = _parse_tshark_rows(tshark_result.output, include_ja3=include_ja3)
        truncated = (
            tshark_result.status is LocalCommandStatus.OUTPUT_TRUNCATED
            or capinfos_result.status is LocalCommandStatus.OUTPUT_TRUNCATED
        )
        limitations: list[str] = []
        if self._ja3_available is False:
            limitations.append("La version installée de TShark n'expose pas le champ JA3.")
        elif self._ja3_available is None:
            limitations.append("La disponibilité du champ JA3 n'a pas pu être établie.")
        if truncated:
            limitations.append("La sortie TShark/capinfos a atteint sa limite explicite.")
        return TsharkInspection(
            status=LocalCommandStatus.OUTPUT_TRUNCATED
            if truncated
            else LocalCommandStatus.SUCCEEDED,
            detail_code="output_limit" if truncated else None,
            tshark_version=tshark.version,
            capinfos_version=capinfos.version,
            packets=packets,
            capinfos_summary=capinfos_result.output,
            output_truncated=truncated,
            ja3_available=self._ja3_available,
            limitations=tuple(limitations),
        )

    def _capability(self, command_id: NetworkCommandId) -> LocalCommandCapability | None:
        return self.catalog.get(cast(LocalCommandId, command_id))

    @property
    def _filesystem_confined(self) -> bool:
        return bool(getattr(self.runner, "filesystem_confined", False))


def _parse_tshark_rows(output: str, *, include_ja3: bool) -> tuple[TsharkPacketFields, ...]:
    rows: list[TsharkPacketFields] = []
    expected_columns = len(TSHARK_FIELDS) + int(include_ja3)
    for cells in csv.reader(io.StringIO(output), delimiter="\t", quotechar='"', escapechar="\\"):
        if not cells or not any(cells):
            continue
        if len(cells) < expected_columns:
            cells.extend([""] * (expected_columns - len(cells)))
        if len(cells) > expected_columns:
            cells = cells[:expected_columns]
        try:
            frame_number = int(cells[0])
        except (ValueError, TypeError):
            continue
        try:
            frame_length = int(cells[2]) if cells[2] else None
        except ValueError:
            frame_length = None
        rows.append(
            TsharkPacketFields(
                frame_number=frame_number,
                timestamp=cells[1] or None,
                frame_length=frame_length,
                protocol=cells[3],
                protocol_stack=cells[4],
                values=tuple(cells[5:]),
            )
        )
    return tuple(rows)

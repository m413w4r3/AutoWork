from __future__ import annotations

import hashlib
import re
from collections import Counter
from io import BytesIO
from math import log2
from typing import Any, cast
from uuid import UUID

import pefile  # type: ignore[import-untyped]
from elftools.elf.elffile import ELFFile

from cti_app.domain.analysis import SampleFeatureSetV1, SampleFormat
from cti_app.domain.classification import TLP
from cti_app.domain.code_features import PackingSignals
from cti_app.infrastructure.non_discriminant_patterns import load_non_discriminant_patterns

_ASCII = re.compile(rb"[ -~]{4,}")
_WIDE = re.compile(rb"(?:[ -~]\x00){4,}")


def build_packing_signals(payload: bytes, recovered_function_count: int) -> PackingSignals:
    executable_sections: list[bytes] = []
    section_names: set[str] = set()
    try:
        if payload.startswith(b"MZ"):
            pe = pefile.PE(data=payload, fast_load=False)
            for section in pe.sections:
                name = section.Name.rstrip(b"\0").decode("ascii", "replace").strip().lower()
                section_names.add(name)
                if section.Characteristics & 0x20000000:
                    data = section.get_data()
                    if data:
                        executable_sections.append(data)
        elif payload.startswith(b"\x7fELF"):
            elf = ELFFile(BytesIO(payload))
            for section in elf.iter_sections():
                name = str(section.name).strip().lower()
                section_names.add(name)
                if section["sh_flags"] & 0x4:
                    data = section.data()
                    if data:
                        executable_sections.append(data)
    except Exception:
        executable_sections = []
        section_names = set()

    executable_bytes = sum(len(data) for data in executable_sections)
    entropy = (
        max(_shannon_entropy(data) for data in executable_sections)
        if executable_sections
        else None
    )
    registry = load_non_discriminant_patterns()
    marker_hits: list[str] = []
    for name in section_names:
        entry = registry.lookup("section", name)
        if entry is not None and entry.category == "upx":
            marker_hits.append(name)
    return PackingSignals(
        max_executable_section_entropy=entropy,
        executable_bytes=executable_bytes,
        recovered_function_count=recovered_function_count,
        executable_bytes_per_function=(
            executable_bytes / recovered_function_count if recovered_function_count > 0 else None
        ),
        known_packer_marker_hits=tuple(sorted(marker_hits)),
    )


def _shannon_entropy(data: bytes) -> float:
    counts = Counter(data)
    size = len(data)
    return -sum((count / size) * log2(count / size) for count in counts.values())


class StaticFeatureExtractor:
    def extract(
        self,
        *,
        sample_id: UUID,
        blob_id: UUID,
        payload: bytes,
        parameters_sha256: str,
        tlp: TLP,
        do_not_submit: bool,
        external_llm_allowed: bool,
        max_strings: int,
        min_string_length: int,
    ) -> SampleFeatureSetV1:
        strings = self._strings(payload, min_string_length, max_strings)
        kwargs: dict[str, Any] = {
            "sections": (),
            "imports": (),
            "exports": (),
            "resources": (),
            "signature": None,
            "imphash": None,
            "rich_header_hash": None,
            "opcode_fragment16": (),
            "partial_errors": (),
        }
        fmt = SampleFormat.UNKNOWN
        errors: list[str] = []
        try:
            if payload.startswith(b"MZ"):
                fmt = SampleFormat.PE
                kwargs.update(self._pe(payload))
            elif payload.startswith(b"\x7fELF"):
                fmt = SampleFormat.ELF
                kwargs.update(self._elf(payload))
            elif payload.lstrip().startswith(b"{\\rtf"):
                fmt = SampleFormat.RTF
        except Exception as exc:
            errors.append(f"{fmt.value.lower()}_parse_failed:{type(exc).__name__}")
        kwargs["partial_errors"] = tuple(errors + list(kwargs["partial_errors"]))
        ssdeep = self._optional_hash("ppdeep", payload)
        tlsh = self._optional_hash("tlsh", payload)
        return SampleFeatureSetV1(
            sample_id=sample_id,
            blob_id=blob_id,
            extractor_version="static-v1",
            parameters_sha256=parameters_sha256,
            format=fmt,
            size=len(payload),
            md5=hashlib.md5(payload).hexdigest(),
            sha1=hashlib.sha1(payload).hexdigest(),
            sha256=hashlib.sha256(payload).hexdigest(),
            strings=tuple(strings),
            ssdeep=ssdeep,
            tlsh=tlsh,
            tlp=tlp,
            do_not_submit=do_not_submit,
            external_llm_allowed=external_llm_allowed,
            **kwargs,
        )

    @staticmethod
    def _strings(payload: bytes, minimum: int, maximum: int) -> list[dict[str, Any]]:
        entries: list[tuple[int, str, str]] = []
        for match in _ASCII.finditer(payload):
            if len(match.group()) >= minimum:
                entries.append((match.start(), "ascii", match.group().decode("utf-8", "replace")))
        for match in _WIDE.finditer(payload):
            value = match.group().decode("utf-16le", "replace")
            if len(value) >= minimum:
                entries.append((match.start(), "utf-16le", value))
        entries.sort()
        counts: dict[tuple[str, str], int] = {}
        for _, encoding, value in entries:
            counts[(encoding, value)] = counts.get((encoding, value), 0) + 1
        return [
            {
                "offset": offset,
                "encoding": encoding,
                "value": value,
                "occurrence_count": counts[(encoding, value)],
            }
            for offset, encoding, value in entries[:maximum]
        ]

    @staticmethod
    def _fragments(data: bytes) -> tuple[str, ...]:
        return tuple(
            fragment[:16].hex() for fragment in re.split(rb"\x00{3,}", data) if len(fragment) >= 8
        )

    def _pe(self, payload: bytes) -> dict[str, object]:
        pe = pefile.PE(data=payload, fast_load=False)
        sections = tuple(
            {
                "name": section.Name.rstrip(b"\0").decode("ascii", "replace"),
                "rva": section.VirtualAddress,
                "size": section.Misc_VirtualSize,
            }
            for section in pe.sections
        )
        imports = tuple(
            sorted(
                f"{entry.dll.decode('ascii', 'replace')}!"
                f"{imp.name.decode('ascii', 'replace') if imp.name else imp.ordinal}"
                for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
                for imp in entry.imports
            )
        )
        exports = tuple(
            sorted(
                symbol.name.decode("ascii", "replace")
                for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])
                if symbol.name
            )
        )
        directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[4]
        rich = pe.parse_rich_header()
        clear = rich.get("clear_data") if rich else None
        entry = pe.OPTIONAL_HEADER.AddressOfEntryPoint
        section = next(
            (
                item
                for item in pe.sections
                if item.VirtualAddress
                <= entry
                < item.VirtualAddress + max(item.Misc_VirtualSize, item.SizeOfRawData)
            ),
            None,
        )
        return {
            "sections": sections,
            "imports": imports,
            "exports": exports,
            "resources": (),
            "signature": {
                "present": bool(directory.VirtualAddress and directory.Size),
                "size": directory.Size,
            },
            "imphash": pe.get_imphash() or None,
            "rich_header_hash": hashlib.md5(clear).hexdigest() if clear else None,
            "opcode_fragment16": self._fragments(section.get_data()) if section else (),
            "partial_errors": (),
        }

    def _elf(self, payload: bytes) -> dict[str, object]:
        elf = ELFFile(BytesIO(payload))
        sections = tuple(
            {"name": section.name, "address": section["sh_addr"], "size": section["sh_size"]}
            for section in elf.iter_sections()
        )
        imports: list[str] = []
        exports: list[str] = []
        for section in elf.iter_sections():
            if section["sh_type"] in ("SHT_SYMTAB", "SHT_DYNSYM"):
                for symbol in cast(Any, section).iter_symbols():
                    if not symbol.name:
                        continue
                    (imports if symbol["st_shndx"] == "SHN_UNDEF" else exports).append(symbol.name)
        entry = elf.header["e_entry"]
        code = next(
            (
                section.data()
                for section in elf.iter_sections()
                if section["sh_addr"] <= entry < section["sh_addr"] + section["sh_size"]
            ),
            b"",
        )
        return {
            "sections": sections,
            "imports": tuple(sorted(set(imports))),
            "exports": tuple(sorted(set(exports))),
            "resources": (),
            "signature": None,
            "imphash": None,
            "rich_header_hash": None,
            "opcode_fragment16": self._fragments(code),
            "partial_errors": (),
        }

    @staticmethod
    def _optional_hash(name: str, payload: bytes) -> str | None:
        try:
            if name == "ppdeep":
                import ppdeep  # type: ignore[import-untyped]

                return str(ppdeep.hash(payload))
            import tlsh  # type: ignore[import-not-found]

            value = tlsh.hash(payload)
            return str(value) if value else None
        except Exception:
            return None

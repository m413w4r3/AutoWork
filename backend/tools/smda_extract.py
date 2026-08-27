"""Small, bounded-process SMDA JSON wrapper.

This file is deliberately the only AutoWork code that imports SMDA.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from smda.common import SmdaFunction
from smda.Disassembler import Disassembler
from smda.SmdaConfig import SmdaConfig


def main(path: Path) -> None:
    report = Disassembler().disassembleFile(str(path))
    architecture = str(getattr(report, "architecture", ""))
    if architecture.lower() == "intel":
        architecture = _infer_intel_architecture(path) or architecture
    escaper = report.getInstructionEscaper()
    base_addr = getattr(report, "base_addr", None)
    binary_size = getattr(report, "binary_size", None)
    escaped_bounds = None
    if (
        isinstance(base_addr, int)
        and not isinstance(base_addr, bool)
        and isinstance(binary_size, int)
        and not isinstance(binary_size, bool)
    ):
        escaped_bounds = (base_addr, base_addr + binary_size)
    functions = []
    for function in report.getFunctions():
        blocks = []
        for block in function.getBlocks():
            instructions = []
            for instruction in block.getInstructions():
                raw_bytes = _json_bytes(instruction.bytes)
                escaped_kwargs = {}
                if escaped_bounds is not None:
                    escaped_kwargs = {
                        "lower_addr": escaped_bounds[0],
                        "upper_addr": escaped_bounds[1],
                    }
                escaped = instruction.getEscapedBinary(escaper, **escaped_kwargs)
                instructions.append(
                    {
                        "offset": int(instruction.offset),
                        "bytes": raw_bytes,
                        "mnemonic": str(instruction.mnemonic),
                        "escaped_bytes": _json_escaped_bytes(escaped, bytes(raw_bytes)),
                    }
                )
            blocks.append({"offset": int(block.offset), "instructions": instructions})
        functions.append({"offset": int(function.offset), "basic_blocks": blocks})
    output = {
        "smda_version": importlib.metadata.version("smda"),
        "escaper_compatibility_version": str(SmdaConfig.ESCAPER_DOWNWARD_COMPATIBILITY),
        "intel_pic_hash_escape_version": _canonical_version(
            SmdaFunction.INTEL_PIC_HASH_ESCAPE_VERSION
        ),
        "architecture": architecture,
        "functions": functions,
    }
    json.dump(output, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


def _canonical_version(value: Any) -> str:
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(component, int) and not isinstance(component, bool) and component >= 0
        for component in value
    ):
        return ".".join(str(component) for component in value)
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError("SMDA version must be a non-empty string or integer components")


def _infer_intel_architecture(path: Path) -> str | None:
    payload = path.read_bytes()
    if payload.startswith(b"MZ") and len(payload) >= 0x40:
        pe_offset = int.from_bytes(payload[0x3C:0x40], "little")
        optional_magic_offset = pe_offset + 24
        if len(payload) >= optional_magic_offset + 2:
            optional_magic = int.from_bytes(
                payload[optional_magic_offset : optional_magic_offset + 2], "little"
            )
            if optional_magic == 0x20B:
                return "intel.64bit"
            if optional_magic == 0x10B:
                return "intel.32bit"
    if payload.startswith(b"\x7fELF") and len(payload) >= 20:
        machine = int.from_bytes(payload[18:20], "little")
        if machine == 0x3E:
            return "intel.64bit" if payload[4] == 2 else None
        if machine == 0x03:
            return "intel.32bit" if payload[4] == 1 else None
    return None


def _json_bytes(value: Any) -> list[int]:
    if isinstance(value, str):
        return list(bytes.fromhex(value))
    return list(bytes(value))


def _json_escaped_bytes(value: Any, raw_bytes: bytes) -> list[Any]:
    if not isinstance(value, str):
        raise ValueError("SMDA escaped bytes must be a string")
    if len(value) != 2 * len(raw_bytes):
        raise ValueError("SMDA escaped bytes length does not match raw bytes")
    output: list[Any] = []
    for index in range(0, len(value), 2):
        pair = value[index : index + 2]
        if pair == "??":
            output.append("??")
            continue
        try:
            output.append(int(pair, 16))
        except ValueError as exc:
            raise ValueError("SMDA escaped bytes contain an unexpected character") from exc
    return output


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smda_extract.py SAMPLE")
    main(Path(sys.argv[1]))

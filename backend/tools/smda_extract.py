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
    escaper = report.getInstructionEscaper()
    functions = []
    for function in report.getFunctions():
        blocks = []
        for block in function.getBlocks():
            instructions = []
            for instruction in block.getInstructions():
                raw_bytes = _json_bytes(instruction.bytes)
                escaped = escaper.escapeBinary(instruction.bytes, instruction.offset)
                instructions.append(
                    {
                        "offset": int(instruction.offset),
                        "bytes": raw_bytes,
                        "mnemonic": str(instruction.mnemonic),
                        "escaped_bytes": _json_escaped_bytes(escaped),
                    }
                )
            blocks.append({"offset": int(block.offset), "instructions": instructions})
        functions.append({"offset": int(function.offset), "basic_blocks": blocks})
    output = {
        "smda_version": importlib.metadata.version("smda"),
        "escaper_compatibility_version": str(SmdaConfig.ESCAPER_DOWNWARD_COMPATIBILITY),
        "intel_pic_hash_escape_version": str(SmdaFunction.INTEL_PIC_HASH_ESCAPE_VERSION),
        "architecture": architecture,
        "functions": functions,
    }
    json.dump(output, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


def _json_bytes(value: Any) -> list[int]:
    if isinstance(value, str):
        return list(bytes.fromhex(value))
    return list(bytes(value))


def _json_escaped_bytes(value: Any) -> list[Any]:
    if isinstance(value, str):
        parts = value.split()
        if len(parts) > 1:
            return [_escaped_token(part) for part in parts]
        if len(value) % 2 == 0:
            try:
                return list(bytes.fromhex(value))
            except ValueError:
                pass
        return [_escaped_token(value)]
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    return [_json_escaped_token(item) for item in value]


def _json_escaped_token(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)) and len(value) == 1:
        return value[0]
    return value


def _escaped_token(value: str) -> Any:
    value = value.strip()
    if value in {"?", "??", "*", "masked", "MASKED"}:
        return "??"
    if len(value) == 2:
        return int(value, 16)
    return value


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smda_extract.py SAMPLE")
    main(Path(sys.argv[1]))

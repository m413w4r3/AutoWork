"""Deterministic, stdlib-only binary fixtures generated in memory for M2 tests.

No binary payload is stored in the repository. Tests call the builders below
and write the returned bytes into pytest's ``tmp_path`` only when a filesystem
path is required by a parser or subprocess.

The PE64 and ELF64 fixtures are intentionally tiny but structurally valid
enough for pefile/pyelftools and for later local-disassembly smoke tests.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

FIXTURE_ASCII_STRING = b"AUTOWORK_FIXTURE_STRING"
FIXTURE_UTF16LE_STRING = "AUTOWORK_WIDE_STRING".encode("utf-16le")

_TEXT_BYTES = bytes.fromhex(
    # Function-like x86-64 sequence, 16 bytes.
    "55 "  # push rbp
    "48 89 e5 "  # mov rbp, rsp
    "48 83 ec 20 "  # sub rsp, 0x20
    "31 c0 "  # xor eax, eax
    "48 83 c4 20 "  # add rsp, 0x20
    "5d "  # pop rbp
    "c3 "  # ret
    # yarGen-compatible fragment separator: >= 3 NUL bytes.
    "00 00 00 "
    # Second fragment: exactly 8 bytes.
    "90 90 90 90 31 c0 c3 90"
)

RICH_XOR_KEY = 0x11223344
RICH_CLEAR_DATA = b"".join(
    struct.pack("<I", value)
    for value in (
        0x536E6144,  # "DanS"
        0,
        0,
        0,
        0x01020003,  # deterministic synthetic comp.id
        1,
    )
)
RICH_CLEAR_MD5 = hashlib.md5(RICH_CLEAR_DATA).hexdigest()


def _rich_encoded_data() -> bytes:
    encoded = bytearray()
    for offset in range(0, len(RICH_CLEAR_DATA), 4):
        value = struct.unpack_from("<I", RICH_CLEAR_DATA, offset)[0]
        encoded += struct.pack("<I", value ^ RICH_XOR_KEY)
    encoded += b"Rich" + struct.pack("<I", RICH_XOR_KEY)
    return bytes(encoded)


def _overlay() -> bytes:
    """Strings live outside executable sections so code-fragment tests stay stable."""
    return (
        b"\x00"
        + FIXTURE_ASCII_STRING
        + b"\x00"
        + FIXTURE_UTF16LE_STRING
        + b"\x00\x00"
    )


def build_pe64(*, rich_header: bool = True) -> bytes:
    """Return a deterministic x86-64 PE image with one executable ``.text`` section."""
    pe_offset = 0x100
    headers_size = 0x400
    text_rva = 0x1000
    text_raw_offset = headers_size
    text_raw_size = 0x200
    file_alignment = 0x200
    section_alignment = 0x1000

    dos = bytearray(pe_offset)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, pe_offset)
    stub = b"This program cannot be run in DOS mode.\r\r\n$"
    dos[0x40 : 0x40 + len(stub)] = stub

    if rich_header:
        rich = _rich_encoded_data()
        dos[0xA0 : 0xA0 + len(rich)] = rich

    coff = struct.pack(
        "<HHIIIHH",
        0x8664,  # IMAGE_FILE_MACHINE_AMD64
        1,  # NumberOfSections
        0,  # TimeDateStamp
        0,  # PointerToSymbolTable
        0,  # NumberOfSymbols
        0xF0,  # SizeOfOptionalHeader (PE32+)
        0x0022,  # EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE
    )

    optional = bytearray()
    optional += struct.pack("<HBB", 0x20B, 14, 0)
    optional += struct.pack("<III", text_raw_size, 0, 0)
    optional += struct.pack("<II", text_rva, text_rva)
    optional += struct.pack("<Q", 0x140000000)
    optional += struct.pack("<II", section_alignment, file_alignment)
    optional += struct.pack("<HHHHHH", 6, 0, 0, 0, 6, 0)
    optional += struct.pack("<I", 0)
    optional += struct.pack("<III", 0x2000, headers_size, 0)
    optional += struct.pack("<HH", 3, 0x8160)
    optional += struct.pack(
        "<QQQQ",
        0x100000,
        0x1000,
        0x100000,
        0x1000,
    )
    optional += struct.pack("<II", 0, 16)
    optional += b"\x00" * (16 * 8)
    assert len(optional) == 0xF0

    section = bytearray(40)
    section[0:8] = b".text\x00\x00\x00"
    struct.pack_into(
        "<IIIIIIHHI",
        section,
        8,
        len(_TEXT_BYTES),
        text_rva,
        text_raw_size,
        text_raw_offset,
        0,
        0,
        0,
        0,
        0x60000020,  # CODE | EXECUTE | READ
    )

    headers = bytes(dos) + b"PE\x00\x00" + coff + bytes(optional) + bytes(section)
    assert len(headers) <= headers_size

    payload = bytearray(headers_size + text_raw_size)
    payload[: len(headers)] = headers
    payload[text_raw_offset : text_raw_offset + len(_TEXT_BYTES)] = _TEXT_BYTES
    payload.extend(_overlay())
    return bytes(payload)


def build_elf64() -> bytes:
    """Return a deterministic x86-64 ELF executable with .text and .shstrtab."""
    text_offset = 0x100
    text_address = 0x400100

    shstr = b"\x00.text\x00.shstrtab\x00"
    shstr_offset = 0x180

    section_table_offset = 0x200
    section_header_size = 64
    section_count = 3
    file_size = section_table_offset + section_header_size * section_count

    ident = bytearray(16)
    ident[0:4] = b"\x7fELF"
    ident[4] = 2  # ELFCLASS64
    ident[5] = 1  # ELFDATA2LSB
    ident[6] = 1  # EV_CURRENT

    elf_header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        bytes(ident),
        2,  # ET_EXEC
        0x3E,  # EM_X86_64
        1,  # EV_CURRENT
        text_address,
        64,  # program-header offset
        section_table_offset,
        0,
        64,  # ELF header size
        56,  # program-header entry size
        1,  # program-header count
        section_header_size,
        section_count,
        2,  # .shstrtab index
    )
    assert len(elf_header) == 64

    program_header = struct.pack(
        "<IIQQQQQQ",
        1,  # PT_LOAD
        5,  # PF_R | PF_X
        0,
        0x400000,
        0x400000,
        section_table_offset,
        section_table_offset,
        0x1000,
    )
    assert len(program_header) == 56

    null_section = bytes(section_header_size)

    text_section = struct.pack(
        "<IIQQQQIIQQ",
        1,  # name offset: ".text"
        1,  # SHT_PROGBITS
        0x6,  # SHF_ALLOC | SHF_EXECINSTR
        text_address,
        text_offset,
        len(_TEXT_BYTES),
        0,
        0,
        16,
        0,
    )

    shstr_section = struct.pack(
        "<IIQQQQIIQQ",
        shstr.index(b".shstrtab"),
        3,  # SHT_STRTAB
        0,
        0,
        shstr_offset,
        len(shstr),
        0,
        0,
        1,
        0,
    )

    payload = bytearray(file_size)
    payload[:64] = elf_header
    payload[64:120] = program_header
    payload[text_offset : text_offset + len(_TEXT_BYTES)] = _TEXT_BYTES
    payload[shstr_offset : shstr_offset + len(shstr)] = shstr

    payload[
        section_table_offset : section_table_offset + 64
    ] = null_section
    payload[
        section_table_offset + 64 : section_table_offset + 128
    ] = text_section
    payload[
        section_table_offset + 128 : section_table_offset + 192
    ] = shstr_section

    payload.extend(_overlay())
    return bytes(payload)


def build_rtf() -> bytes:
    """Return a deterministic minimal RTF document."""
    return b"{\\rtf1\\ansi AUTOWORK_FIXTURE_STRING\\par}\n"


def build_unknown() -> bytes:
    """Return bytes that intentionally match no supported executable/document format."""
    return b"AUTOWORK_UNKNOWN\x00fixture\x01\x02\x03"


def build_truncated_pe() -> bytes:
    """Return an MZ-prefixed but invalid/truncated PE for partial-error tests."""
    payload = bytearray(64)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x100)
    return bytes(payload)


def expected_opcode_fragments() -> tuple[str, ...]:
    """yarGen-compatible fragments expected from the executable section bytes."""
    return (
        _TEXT_BYTES[:16].hex(),
        _TEXT_BYTES[19:27].hex(),
    )


def write_fixture(directory: Path, name: str, payload: bytes) -> Path:
    """Write generated bytes under a caller-owned temporary directory."""
    path = directory / name
    path.write_bytes(payload)
    return path
from __future__ import annotations

import ctypes
import os
import platform
import struct


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(_SockFilter))]


def _denied_network_syscalls() -> tuple[int, ...]:
    if os.name != "posix" or platform.system() != "Linux":
        raise OSError("Network syscall filtering is only implemented on Linux")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return (41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 54, 55, 288, 425, 426, 427)
    elif machine in {"aarch64", "arm64"}:
        return (
            198,
            200,
            201,
            202,
            203,
            204,
            205,
            206,
            207,
            208,
            209,
            210,
            211,
            212,
            242,
            425,
            426,
            427,
        )
    raise OSError(f"Unsupported Linux architecture for network filtering: {machine}")


def network_seccomp_filter_program() -> bytes:
    """Serialize the network-denying BPF program for ``bwrap --seccomp``."""
    instructions = [(0x20, 0, 0, 0)]  # load seccomp_data.nr
    for syscall_number in _denied_network_syscalls():
        instructions.extend(
            (
                (0x15, 0, 1, syscall_number),  # if nr matches, return EPERM
                (0x06, 0, 0, 0x00050000 | 1),
            )
        )
    instructions.append((0x06, 0, 0, 0x7FFF0000))  # allow the rest
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


def install_network_seccomp_filter() -> None:
    """Deny Linux networking syscalls in the current process with EPERM."""
    program_bytes = network_seccomp_filter_program()
    instruction_size = ctypes.sizeof(_SockFilter)
    if len(program_bytes) % instruction_size:
        raise OSError("Invalid serialized network seccomp program")
    instruction_count = len(program_bytes) // instruction_size
    filter_array = (_SockFilter * instruction_count).from_buffer_copy(program_bytes)
    program = _SockFprog(instruction_count, filter_array)
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.restype = ctypes.c_int
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Could not set no_new_privs")
    if prctl(22, 2, ctypes.byref(program), 0, 0) != 0:  # PR_SET_SECCOMP / FILTER
        raise OSError(ctypes.get_errno(), "Could not install network seccomp filter")

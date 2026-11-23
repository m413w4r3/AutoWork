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


_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
# CLONE_NEWCGROUP|NEWNS|NEWUTS|NEWIPC|NEWUSER|NEWPID|NEWNET: a confined script
# never creates namespaces; refusing them keeps the container boundary intact.
_CLONE_NAMESPACE_MASK = 0x7E020000


def _serialize(instructions: list[tuple[int, int, int, int]]) -> bytes:
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


def network_seccomp_filter_program() -> bytes:
    """Serialize the network-denying BPF program for ``bwrap --seccomp``."""
    instructions = [(0x20, 0, 0, 0)]  # load seccomp_data.nr
    for syscall_number in _denied_network_syscalls():
        instructions.extend(
            (
                (0x15, 0, 1, syscall_number),  # if nr matches, return EPERM
                (0x06, 0, 0, _SECCOMP_RET_ERRNO | 1),
            )
        )
    instructions.append((0x06, 0, 0, _SECCOMP_RET_ALLOW))  # allow the rest
    return _serialize(instructions)


def _script_denied_syscalls() -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (EPERM syscalls, ENOSYS syscalls) for one agent script.

    The script must not reach the network, signal the trusted supervisor or
    worker, attach to them, or create namespaces/mounts.  ``clone3`` returns
    ENOSYS so glibc falls back to ``clone`` (whose namespace flags are masked
    below), exactly like a kernel without clone3.
    """
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return (
            (
                62,  # kill
                101,  # ptrace
                109,  # setpgid
                112,  # setsid
                129,  # rt_sigqueueinfo
                155,  # pivot_root
                161,  # chroot
                165,  # mount
                166,  # umount2
                200,  # tkill
                234,  # tgkill
                272,  # unshare
                297,  # rt_tgsigqueueinfo
                308,  # setns
                310,  # process_vm_readv
                311,  # process_vm_writev
                424,  # pidfd_send_signal
                434,  # pidfd_open
                438,  # pidfd_getfd
            ),
            (435,),  # clone3
        )
    if machine in {"aarch64", "arm64"}:
        return (
            (
                39,  # umount2
                40,  # mount
                41,  # pivot_root
                51,  # chroot
                97,  # unshare
                117,  # ptrace
                129,  # kill
                130,  # tkill
                131,  # tgkill
                138,  # rt_sigqueueinfo
                154,  # setpgid
                157,  # setsid
                240,  # rt_tgsigqueueinfo
                268,  # setns
                270,  # process_vm_readv
                271,  # process_vm_writev
                424,  # pidfd_send_signal
                434,  # pidfd_open
                438,  # pidfd_getfd
            ),
            (435,),  # clone3
        )
    raise OSError(f"Unsupported Linux architecture for script filtering: {machine}")


def script_seccomp_filter_program() -> bytes:
    """Serialize the full script filter (network + process-chain denials)."""
    if os.name != "posix" or platform.system() != "Linux":
        raise OSError("Script syscall filtering is only implemented on Linux")
    denied, enosys = _script_denied_syscalls()
    clone_number = 220 if platform.machine().lower() in {"aarch64", "arm64"} else 56
    blocked = set(_denied_network_syscalls()) | set(denied)
    instructions = [(0x20, 0, 0, 0)]  # load seccomp_data.nr
    for syscall_number in sorted(blocked):
        instructions.extend(
            (
                (0x15, 0, 1, syscall_number),
                (0x06, 0, 0, _SECCOMP_RET_ERRNO | 1),
            )
        )
    for syscall_number in enosys:
        instructions.extend(
            (
                (0x15, 0, 1, syscall_number),
                (0x06, 0, 0, _SECCOMP_RET_ERRNO | 38),  # ENOSYS: glibc falls back
            )
        )
    # clone(flags, ...) with any namespace flag is refused; the syscall number
    # is architecture-specific but flags are always the first argument.
    instructions.extend(
        (
            (0x15, 0, 4, clone_number),
            (0x20, 0, 0, 16),  # seccomp_data.args[0] (low 32 bits)
            (0x54, 0, 0, _CLONE_NAMESPACE_MASK),
            (0x15, 1, 0, 0),
            (0x06, 0, 0, _SECCOMP_RET_ERRNO | 1),
        )
    )
    instructions.append((0x06, 0, 0, _SECCOMP_RET_ALLOW))
    return _serialize(instructions)


def install_network_seccomp_filter() -> None:
    """Deny Linux networking syscalls in the current process with EPERM."""
    _install_filter(network_seccomp_filter_program(), "network")


def install_script_seccomp_filter() -> None:
    """Deny network, signal, namespace and tracing syscalls in the script."""
    _install_filter(script_seccomp_filter_program(), "script")


def _install_filter(program_bytes: bytes, label: str) -> None:
    instruction_size = ctypes.sizeof(_SockFilter)
    if len(program_bytes) % instruction_size:
        raise OSError("Invalid serialized seccomp program")
    instruction_count = len(program_bytes) // instruction_size
    filter_array = (_SockFilter * instruction_count).from_buffer_copy(program_bytes)
    program = _SockFprog(instruction_count, filter_array)
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.restype = ctypes.c_int
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Could not set no_new_privs")
    if prctl(22, 2, ctypes.byref(program), 0, 0) != 0:  # PR_SET_SECCOMP / FILTER
        raise OSError(ctypes.get_errno(), f"Could not install {label} seccomp filter")

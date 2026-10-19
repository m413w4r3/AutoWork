"""Small passive PCAP/PCAPNG reader for bounded TCP/UDP evidence extraction."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkTransport

_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1),
    b"\xa1\xb2\x3c\x4d": (">", 1),
}
_PCAPNG_SECTION = b"\x0a\x0d\x0d\x0a"
# Ethernet, raw IP, Linux cooked capture v1, and Linux cooked capture v2.
_SUPPORTED_LINK_TYPES = frozenset({1, 12, 101, 113, 276})


class PcapReaderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CapturedPacket:
    frame_number: int
    timestamp_ns: int | None
    captured_length: int
    original_length: int
    link_type: int
    raw_frame: bytes
    transport: NetworkTransport | None
    network_protocol: str | None
    source_address: str | None
    destination_address: str | None
    source_port: int | None
    destination_port: int | None
    payload: bytes
    tcp_sequence: int | None = None
    tcp_acknowledgement: int | None = None
    tcp_flags: int = 0

    @property
    def truncated(self) -> bool:
        return self.captured_length < self.original_length


@dataclass(frozen=True, slots=True)
class PcapReadResult:
    format: NetworkCaptureFormat
    packets: tuple[CapturedPacket, ...]
    link_types: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Interface:
    link_type: int
    snap_length: int
    timestamp_units_per_second: int


def read_capture(data: bytes, *, max_packets: int = 100_000) -> PcapReadResult:
    if max_packets < 1:
        raise ValueError("max_packets must be positive")
    if len(data) < 4:
        raise PcapReaderError("invalid_magic", "Capture is too short to identify its format")
    if data[:4] in _PCAP_MAGICS:
        return _read_pcap(data, max_packets=max_packets)
    if data[:4] == _PCAPNG_SECTION:
        return _read_pcapng(data, max_packets=max_packets)
    raise PcapReaderError("invalid_magic", "Capture magic is not PCAP or PCAPNG")


def _read_pcap(data: bytes, *, max_packets: int) -> PcapReadResult:
    byte_order, fractional_ns = _PCAP_MAGICS[data[:4]]
    if len(data) < 24:
        raise PcapReaderError("corrupt_capture", "PCAP global header is incomplete")
    major = _u16(data, 4, byte_order)
    minor = _u16(data, 6, byte_order)
    snap_length = _u32(data, 16, byte_order)
    link_type = _u32(data, 20, byte_order) & 0xFFFF
    if major != 2 or minor > 4 or snap_length <= 0:
        raise PcapReaderError("corrupt_capture", "PCAP global header values are invalid")
    _check_link_type(link_type)

    packets: list[CapturedPacket] = []
    offset = 24
    while offset < len(data):
        if len(data) - offset < 16:
            raise PcapReaderError("corrupt_capture", "PCAP packet header is incomplete")
        seconds = _u32(data, offset, byte_order)
        fraction = _u32(data, offset + 4, byte_order)
        captured_length = _u32(data, offset + 8, byte_order)
        original_length = _u32(data, offset + 12, byte_order)
        offset += 16
        if captured_length > snap_length or original_length < captured_length:
            raise PcapReaderError("corrupt_capture", "PCAP packet lengths are inconsistent")
        if fraction >= (1_000_000_000 // fractional_ns):
            raise PcapReaderError("corrupt_capture", "PCAP timestamp fraction is invalid")
        if captured_length > len(data) - offset:
            raise PcapReaderError("corrupt_capture", "PCAP packet data is incomplete")
        if len(packets) >= max_packets:
            raise PcapReaderError("packet_limit", "Capture exceeds the packet processing limit")
        frame = data[offset : offset + captured_length]
        offset += captured_length
        timestamp_ns = seconds * 1_000_000_000 + fraction * fractional_ns
        packets.append(
            _packet(
                len(packets) + 1,
                timestamp_ns,
                captured_length,
                original_length,
                link_type,
                frame,
            )
        )
    return PcapReadResult(NetworkCaptureFormat.PCAP, tuple(packets), (link_type,))


def _read_pcapng(data: bytes, *, max_packets: int) -> PcapReadResult:
    offset = 0
    byte_order: str | None = None
    interfaces: list[_Interface] = []
    packets: list[CapturedPacket] = []
    link_types: set[int] = set()
    while offset < len(data):
        if len(data) - offset < 12:
            raise PcapReaderError("corrupt_capture", "PCAPNG block header is incomplete")
        raw_type = data[offset : offset + 4]
        if raw_type == _PCAPNG_SECTION:
            if len(data) - offset < 28:
                raise PcapReaderError("corrupt_capture", "PCAPNG section header is incomplete")
            byte_order_magic = data[offset + 8 : offset + 12]
            if byte_order_magic == b"\x4d\x3c\x2b\x1a":
                byte_order = "<"
            elif byte_order_magic == b"\x1a\x2b\x3c\x4d":
                byte_order = ">"
            else:
                raise PcapReaderError("corrupt_capture", "PCAPNG byte order magic is invalid")
            interfaces = []
            block_type = 0x0A0D0D0A
        elif byte_order is None:
            raise PcapReaderError("corrupt_capture", "PCAPNG section header must be first")
        else:
            block_type = int.from_bytes(raw_type, "little" if byte_order == "<" else "big")

        assert byte_order is not None
        block_length = _u32(data, offset + 4, byte_order)
        if block_length < 12 or block_length % 4 or block_length > len(data) - offset:
            raise PcapReaderError("corrupt_capture", "PCAPNG block length is invalid")
        if _u32(data, offset + block_length - 4, byte_order) != block_length:
            raise PcapReaderError("corrupt_capture", "PCAPNG block lengths do not agree")
        body_start = offset + 8
        body_end = offset + block_length - 4

        if block_type == 0x0A0D0D0A:
            if block_length < 28:
                raise PcapReaderError("corrupt_capture", "PCAPNG section header is too small")
            major = _u16(data, body_start + 4, byte_order)
            if major != 1:
                raise PcapReaderError("corrupt_capture", "PCAPNG section version is unsupported")
        elif block_type == 1:  # Interface Description Block
            if body_end - body_start < 8:
                raise PcapReaderError("corrupt_capture", "PCAPNG interface block is incomplete")
            link_type = _u16(data, body_start, byte_order)
            _check_link_type(link_type)
            snap_length = _u32(data, body_start + 4, byte_order)
            if snap_length <= 0:
                raise PcapReaderError("corrupt_capture", "PCAPNG snap length is invalid")
            timestamp_units = 1_000_000
            for code, value in _options(data, body_start + 8, body_end, byte_order):
                if code == 9 and value:
                    raw_resolution = value[0]
                    exponent = raw_resolution & 0x7F
                    if exponent > 63:
                        raise PcapReaderError(
                            "corrupt_capture", "PCAPNG time resolution is invalid"
                        )
                    base = 2 if raw_resolution & 0x80 else 10
                    timestamp_units = base**exponent
            interfaces.append(_Interface(link_type, snap_length, timestamp_units))
            link_types.add(link_type)
        elif block_type == 6:  # Enhanced Packet Block
            if body_end - body_start < 20:
                raise PcapReaderError(
                    "corrupt_capture", "PCAPNG enhanced packet block is incomplete"
                )
            interface_id = _u32(data, body_start, byte_order)
            if interface_id >= len(interfaces):
                raise PcapReaderError(
                    "corrupt_capture", "PCAPNG packet references an unknown interface"
                )
            interface = interfaces[interface_id]
            timestamp_ticks = (_u32(data, body_start + 4, byte_order) << 32) | _u32(
                data, body_start + 8, byte_order
            )
            captured_length = _u32(data, body_start + 12, byte_order)
            original_length = _u32(data, body_start + 16, byte_order)
            data_start = body_start + 20
            padded_length = (captured_length + 3) & ~3
            if (
                captured_length > interface.snap_length
                or original_length < captured_length
                or data_start + padded_length > body_end
            ):
                raise PcapReaderError("corrupt_capture", "PCAPNG packet lengths are inconsistent")
            _check_packet_limit(packets, max_packets)
            frame = data[data_start : data_start + captured_length]
            timestamp_ns = timestamp_ticks * 1_000_000_000 // interface.timestamp_units_per_second
            packets.append(
                _packet(
                    len(packets) + 1,
                    timestamp_ns,
                    captured_length,
                    original_length,
                    interface.link_type,
                    frame,
                )
            )
        elif block_type == 3:  # Simple Packet Block (timestamp unavailable)
            if not interfaces or body_end - body_start < 4:
                raise PcapReaderError("corrupt_capture", "PCAPNG simple packet block is incomplete")
            interface = interfaces[0]
            original_length = _u32(data, body_start, byte_order)
            captured_length = min(original_length, interface.snap_length, body_end - body_start - 4)
            _check_packet_limit(packets, max_packets)
            frame = data[body_start + 4 : body_start + 4 + captured_length]
            packets.append(
                _packet(
                    len(packets) + 1,
                    None,
                    captured_length,
                    original_length,
                    interface.link_type,
                    frame,
                )
            )
        offset += block_length

    if byte_order is None:
        raise PcapReaderError("corrupt_capture", "PCAPNG section header is missing")
    return PcapReadResult(
        NetworkCaptureFormat.PCAPNG,
        tuple(packets),
        tuple(sorted(link_types)),
    )


def _packet(
    frame_number: int,
    timestamp_ns: int | None,
    captured_length: int,
    original_length: int,
    link_type: int,
    frame: bytes,
) -> CapturedPacket:
    parsed = _decode_transport(frame, link_type)
    return CapturedPacket(
        frame_number=frame_number,
        timestamp_ns=timestamp_ns,
        captured_length=captured_length,
        original_length=original_length,
        link_type=link_type,
        raw_frame=frame,
        transport=parsed[0] if parsed else None,
        network_protocol=parsed[1] if parsed else None,
        source_address=parsed[2] if parsed else None,
        destination_address=parsed[3] if parsed else None,
        source_port=parsed[4] if parsed else None,
        destination_port=parsed[5] if parsed else None,
        payload=parsed[6] if parsed else b"",
        tcp_sequence=parsed[7] if parsed else None,
        tcp_acknowledgement=parsed[8] if parsed else None,
        tcp_flags=parsed[9] if parsed else 0,
    )


def _decode_transport(
    frame: bytes, link_type: int
) -> (
    tuple[
        NetworkTransport, str, str, str, int | None, int | None, bytes, int | None, int | None, int
    ]
    | None
):
    if link_type == 1:
        if len(frame) < 14:
            return None
        ether_type = int.from_bytes(frame[12:14], "big")
        offset = 14
    elif link_type == 113:  # Linux cooked capture v1 (SLL)
        if len(frame) < 16:
            return None
        ether_type = int.from_bytes(frame[14:16], "big")
        offset = 16
    elif link_type == 276:  # Linux cooked capture v2 (SLL2)
        if len(frame) < 20:
            return None
        ether_type = int.from_bytes(frame[:2], "big")
        offset = 20
    else:
        offset = 0
        if not frame:
            return None
        version = frame[0] >> 4
        ether_type = 0x0800 if version == 4 else 0x86DD if version == 6 else 0

    if link_type in {1, 113, 276}:
        vlan_count = 0
        while ether_type in {0x8100, 0x88A8, 0x9100} and vlan_count < 4:
            if len(frame) < offset + 4:
                return None
            ether_type = int.from_bytes(frame[offset + 2 : offset + 4], "big")
            offset += 4
            vlan_count += 1

    if ether_type == 0x0800:
        parsed = _decode_ipv4(frame, offset)
    elif ether_type == 0x86DD:
        parsed = _decode_ipv6(frame, offset)
    else:
        return None
    if parsed is None:
        return None
    ip_protocol, source, destination, transport_offset, transport_length = parsed
    if ip_protocol == 6:
        if len(frame) < transport_offset + 20 or transport_length < 20:
            return None
        source_port = _u16(frame, transport_offset, ">")
        destination_port = _u16(frame, transport_offset + 2, ">")
        sequence = _u32(frame, transport_offset + 4, ">")
        acknowledgement = _u32(frame, transport_offset + 8, ">")
        header_length = (frame[transport_offset + 12] >> 4) * 4
        if header_length < 20 or header_length > transport_length:
            return None
        flags = ((frame[transport_offset + 12] & 1) << 8) | frame[transport_offset + 13]
        start = transport_offset + header_length
        end = min(len(frame), transport_offset + transport_length)
        return (
            NetworkTransport.TCP,
            "tcp",
            source,
            destination,
            source_port,
            destination_port,
            frame[start:end] if start < end else b"",
            sequence,
            acknowledgement,
            flags,
        )
    if ip_protocol == 17:
        if len(frame) < transport_offset + 8 or transport_length < 8:
            return None
        source_port = _u16(frame, transport_offset, ">")
        destination_port = _u16(frame, transport_offset + 2, ">")
        udp_length = _u16(frame, transport_offset + 4, ">")
        if udp_length < 8:
            return None
        start = transport_offset + 8
        end = min(len(frame), transport_offset + min(transport_length, udp_length))
        return (
            NetworkTransport.UDP,
            "udp",
            source,
            destination,
            source_port,
            destination_port,
            frame[start:end] if start < end else b"",
            None,
            None,
            0,
        )
    return (
        NetworkTransport.OTHER,
        _ip_protocol_name(ip_protocol),
        source,
        destination,
        None,
        None,
        b"",
        None,
        None,
        0,
    )


def _decode_ipv4(frame: bytes, offset: int) -> tuple[int, str, str, int, int] | None:
    if len(frame) < offset + 20 or frame[offset] >> 4 != 4:
        return None
    header_length = (frame[offset] & 0x0F) * 4
    total_length = _u16(frame, offset + 2, ">")
    if header_length < 20 or total_length < header_length or len(frame) < offset + header_length:
        return None
    fragment = _u16(frame, offset + 6, ">")
    if fragment & 0x3FFF:
        return None
    source = str(ipaddress.IPv4Address(frame[offset + 12 : offset + 16]))
    destination = str(ipaddress.IPv4Address(frame[offset + 16 : offset + 20]))
    transport_offset = offset + header_length
    transport_length = max(0, min(total_length, len(frame) - offset) - header_length)
    return frame[offset + 9], source, destination, transport_offset, transport_length


def _decode_ipv6(frame: bytes, offset: int) -> tuple[int, str, str, int, int] | None:
    if len(frame) < offset + 40 or frame[offset] >> 4 != 6:
        return None
    payload_length = _u16(frame, offset + 4, ">")
    next_header = frame[offset + 6]
    source = str(ipaddress.IPv6Address(frame[offset + 8 : offset + 24]))
    destination = str(ipaddress.IPv6Address(frame[offset + 24 : offset + 40]))
    cursor = offset + 40
    packet_end = min(len(frame), cursor + payload_length)
    for _ in range(8):
        if next_header in {0, 43, 60, 135}:
            if cursor + 2 > packet_end:
                return None
            extension_length = (frame[cursor + 1] + 1) * 8
            next_header = frame[cursor]
        elif next_header == 51:
            if cursor + 2 > packet_end:
                return None
            extension_length = (frame[cursor + 1] + 2) * 4
            next_header = frame[cursor]
        elif next_header == 44:
            if cursor + 8 > packet_end:
                return None
            return None
        else:
            return next_header, source, destination, cursor, max(0, packet_end - cursor)
        if cursor + extension_length > packet_end:
            return None
        cursor += extension_length
    return None


def _options(data: bytes, offset: int, end: int, byte_order: str) -> tuple[tuple[int, bytes], ...]:
    values: list[tuple[int, bytes]] = []
    while offset < end:
        if end - offset < 4:
            raise PcapReaderError("corrupt_capture", "PCAPNG option header is incomplete")
        code = _u16(data, offset, byte_order)
        length = _u16(data, offset + 2, byte_order)
        offset += 4
        padded_length = (length + 3) & ~3
        if offset + padded_length > end:
            raise PcapReaderError("corrupt_capture", "PCAPNG option length is invalid")
        if code == 0:
            if length != 0:
                raise PcapReaderError("corrupt_capture", "PCAPNG end option is malformed")
            break
        values.append((code, data[offset : offset + length]))
        offset += padded_length
    return tuple(values)


def _check_packet_limit(packets: list[CapturedPacket], max_packets: int) -> None:
    if len(packets) >= max_packets:
        raise PcapReaderError("packet_limit", "Capture exceeds the packet processing limit")


def _check_link_type(link_type: int) -> None:
    if link_type not in _SUPPORTED_LINK_TYPES:
        raise PcapReaderError("unsupported_link_type", "Capture link type is not supported")


def _ip_protocol_name(protocol: int) -> str:
    return {1: "icmp", 2: "igmp", 58: "icmpv6"}.get(protocol, f"ip_proto_{protocol}")


def _u16(data: bytes, offset: int, byte_order: str) -> int:
    return int.from_bytes(data[offset : offset + 2], "little" if byte_order == "<" else "big")


def _u32(data: bytes, offset: int, byte_order: str) -> int:
    return int.from_bytes(data[offset : offset + 4], "little" if byte_order == "<" else "big")

"""A small OSC 1.0 codec for the X32's UDP replies."""

from __future__ import annotations

import struct


def encode_message(address: str, args: list[object] | None = None) -> bytes:
    """Pack one OSC message. Callers that query the console pass no arguments."""
    payload = b""
    tags = ","
    for arg in args or []:
        if isinstance(arg, str):
            tags += "s"
            payload += _osc_string(arg)
        elif isinstance(arg, bool):
            tags += "T" if arg else "F"
        elif isinstance(arg, int):
            tags += "i"
            payload += struct.pack(">i", arg)
        elif isinstance(arg, float):
            tags += "f"
            payload += struct.pack(">f", arg)
        elif isinstance(arg, (bytes, bytearray)):
            tags += "b"
            raw = bytes(arg)
            payload += struct.pack(">i", len(raw)) + _pad(raw)
        else:
            raise TypeError(f"Cannot encode {type(arg).__name__}")
    return _osc_string(address) + _osc_string(tags) + payload


def decode_all(data: bytes) -> list[tuple[str, list[object]]]:
    """Return every message in a packet. The X32 sometimes wraps replies in a bundle."""
    if data.startswith(b"#bundle\x00"):
        messages: list[tuple[str, list[object]]] = []
        offset = 16
        while offset + 4 <= len(data):
            size = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            if size < 0 or offset + size > len(data):
                break
            messages.extend(decode_all(data[offset : offset + size]))
            offset += size
        return messages
    return [decode_message(data)]


def decode_message(data: bytes) -> tuple[str, list[object]]:
    address, offset = _read_string(data, 0)
    if offset >= len(data):
        return address, []
    tags, offset = _read_string(data, offset)
    if not tags.startswith(","):
        return address, []
    args: list[object] = []
    for tag in tags[1:]:
        if tag == "s":
            value, offset = _read_string(data, offset)
            args.append(value)
        elif tag == "i":
            args.append(struct.unpack_from(">i", data, offset)[0])
            offset += 4
        elif tag == "f":
            args.append(struct.unpack_from(">f", data, offset)[0])
            offset += 4
        elif tag == "b":
            size = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            blob = data[offset : offset + size]
            args.append(blob)
            offset += size
            offset = (offset + 3) & ~3
        elif tag in {"T", "F"}:
            args.append(tag == "T")
        else:
            raise ValueError(f"Unsupported OSC type {tag}")
    return address, args


def parse_meter_blob(blob: bytes) -> list[float]:
    """X32 meter blobs are a little-endian count followed by little-endian linear floats."""
    if len(blob) < 4:
        return []
    count = struct.unpack_from("<i", blob, 0)[0]
    count = max(0, min(count, (len(blob) - 4) // 4))
    if count == 0:
        return []
    return list(struct.unpack_from("<" + "f" * count, blob, 4))


def _osc_string(text: str) -> bytes:
    return _pad(text.encode("utf-8") + b"\x00")


def _pad(raw: bytes) -> bytes:
    extra = (4 - (len(raw) % 4)) % 4
    return raw + b"\x00" * extra


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    text = data[offset:end].decode("utf-8", "replace")
    return text, (end + 4) & ~3

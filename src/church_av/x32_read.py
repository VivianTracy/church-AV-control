"""Read-only Behringer X32 client.

OBS still receives one combined X-USB feed. This client watches the mixer
separately over OSC. It never sends a fader, a mute, or a name.
"""

from __future__ import annotations

import math
import re
import socket
import time

from church_av.models import BoardReading, StripReading
from church_av.oscutil import decode_all, encode_message, parse_meter_blob
from church_av.runbook import Runbook

# Unofficial X32 OSC remote protocol, firmware 4.x:
# /meters/0 is 70 linear floats: 32 channels, 8 aux, 8 fx, 16 buses, 6 matrices.
# /meters/2 is 49 linear floats. Main L, R, and mono sit at indexes 22, 23, and 24.
METER_TIME_FACTOR = 40
_LABEL_REFRESH_SECONDS = 30.0
_FACTORY_NAME = re.compile(r"^ch\s*0*(\d+)$", re.IGNORECASE)


def linear_to_db(level: float) -> float:
    """X32 meters are linear, with 1.0 at digital full scale."""
    if level <= 1e-8:
        return -100.0
    return 20.0 * math.log10(level)


def usable_name(name: str, index: int) -> str:
    """A factory scribble such as Ch17 is not a name the operator chose."""
    cleaned = " ".join(name.split())
    match = _FACTORY_NAME.fullmatch(cleaned)
    if not cleaned or (match is not None and int(match.group(1)) == index):
        return ""
    return cleaned


class UdpTransport:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.connect((host, port))
        self._timeout = timeout

    def send(self, packet: bytes) -> None:
        self._sock.send(packet)

    def recv(self, timeout: float) -> bytes | None:
        self._sock.settimeout(timeout if timeout > 0 else self._timeout)
        try:
            return self._sock.recv(65535)
        except (TimeoutError, socket.timeout):
            return None

    def close(self) -> None:
        self._sock.close()


class X32Reader:
    """Collect channel, bus, matrix, and main meters, plus scribble names."""

    def __init__(self, host: str = "", port: int = 10023, timeout: float = 0.3, transport=None) -> None:
        self.host = host
        self.port = port
        self._timeout = timeout
        self._transport = transport
        self._owned: UdpTransport | None = None
        self._labels_at = 0.0
        self._channel_names = [""] * 32
        self._channel_meters: list[float | None] = [None] * 32
        self._channel_on: list[bool | None] = [None] * 32
        self._bus_names = [""] * 16
        self._bus_meters: list[float | None] = [None] * 16
        self._matrix_names = [""] * 6
        self._matrix_meters: list[float | None] = [None] * 6
        self._main_name = ""
        self._main_meters: list[float | None] = [None, None, None]

    def close(self) -> None:
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    def read(self, runbook: Runbook | None = None) -> BoardReading:
        """Poll the console. `runbook` is accepted so the booth can call this beside ObsReader."""
        del runbook
        if self._transport is None and not self.host:
            return BoardReading(connected=False, error="X32_HOST is empty")
        try:
            link = self._link()
            self._subscribe(link)
            if time.monotonic() - self._labels_at >= _LABEL_REFRESH_SECONDS:
                self._ask_labels(link)
                self._labels_at = time.monotonic()
            saw = self._collect(link)
        except OSError as exc:
            return BoardReading(connected=False, error=str(exc).strip() or exc.__class__.__name__)
        if not saw:
            return BoardReading(connected=False, error="The X32 did not answer on OSC")
        return self._reading()

    def _link(self):
        if self._transport is not None:
            return self._transport
        if self._owned is None:
            self._owned = UdpTransport(self.host, self.port, self._timeout)
        return self._owned

    def _subscribe(self, link) -> None:
        link.send(encode_message("/xremote"))
        link.send(encode_message("/meters", ["/meters/0", METER_TIME_FACTOR]))
        link.send(encode_message("/meters", ["/meters/2", METER_TIME_FACTOR]))

    def _ask_labels(self, link) -> None:
        """Query names and channel on/off. No argument is sent, so nothing is written."""
        for index in range(1, 33):
            link.send(encode_message(f"/ch/{index:02d}/config/name"))
            link.send(encode_message(f"/ch/{index:02d}/mix/on"))
        for index in range(1, 17):
            link.send(encode_message(f"/bus/{index:02d}/config/name"))
        for index in range(1, 7):
            link.send(encode_message(f"/mtx/{index:02d}/config/name"))
        link.send(encode_message("/main/st/config/name"))

    def _collect(self, link) -> bool:
        """Read the reply burst. Stop after a short gap, and never wait past the poll budget."""
        saw = False
        deadline = time.monotonic() + max(self._timeout, 0.35)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return saw
            wait = remaining if not saw else min(0.12, remaining)
            packet = link.recv(wait)
            if not packet:
                return saw
            saw = True
            for address, args in decode_all(packet):
                self._ingest(address, args)

    def _ingest(self, address: str, args: list[object]) -> None:
        if address in {"/meters/0", "/meters/2"} and args and isinstance(args[0], (bytes, bytearray)):
            self._ingest_meters(int(address.rsplit("/", 1)[-1]), parse_meter_blob(bytes(args[0])))
            return
        name_match = re.fullmatch(r"/(ch|bus|mtx)/(\d+)/config/name", address)
        if name_match and args and isinstance(args[0], str):
            self._store_name(name_match.group(1), int(name_match.group(2)), args[0])
            return
        on_match = re.fullmatch(r"/ch/(\d+)/mix/on", address)
        if on_match and args and args[0] in {0, 1}:
            index = int(on_match.group(1))
            if 1 <= index <= 32:
                self._channel_on[index - 1] = args[0] == 1
            return
        if address == "/main/st/config/name" and args and isinstance(args[0], str):
            self._main_name = usable_name(args[0], 0)

    def _store_name(self, prefix: str, index: int, raw: str) -> None:
        cleaned = usable_name(raw, index)
        if prefix == "ch" and 1 <= index <= 32:
            self._channel_names[index - 1] = cleaned
        elif prefix == "bus" and 1 <= index <= 16:
            self._bus_names[index - 1] = cleaned
        elif prefix == "mtx" and 1 <= index <= 6:
            self._matrix_names[index - 1] = cleaned

    def _ingest_meters(self, bank: int, values: list[float]) -> None:
        if bank == 0:
            self._take(self._channel_meters, values[0:32])
            if len(values) >= 64:
                self._take(self._bus_meters, values[48:64])
            if len(values) >= 70:
                self._take(self._matrix_meters, values[64:70])
            return
        if bank == 2 and len(values) >= 25:
            self._main_meters = [linear_to_db(values[22]), linear_to_db(values[23]), linear_to_db(values[24])]
            if not any(item is not None for item in self._bus_meters):
                self._take(self._bus_meters, values[0:16])
            if not any(item is not None for item in self._matrix_meters) and len(values) >= 22:
                self._take(self._matrix_meters, values[16:22])

    def _take(self, dest: list[float | None], values: list[float]) -> None:
        for index, value in enumerate(values):
            if index < len(dest):
                dest[index] = linear_to_db(value)

    def _reading(self) -> BoardReading:
        return BoardReading(
            connected=True,
            channels=[
                StripReading(
                    "channel",
                    index,
                    self._channel_names[index - 1],
                    self._channel_meters[index - 1],
                    self._channel_on[index - 1],
                )
                for index in range(1, 33)
            ],
            buses=[
                StripReading("bus", index, self._bus_names[index - 1], self._bus_meters[index - 1])
                for index in range(1, 17)
            ],
            matrices=[
                StripReading("matrix", index, self._matrix_names[index - 1], self._matrix_meters[index - 1])
                for index in range(1, 7)
            ],
            main=[
                StripReading("output", 1, self._main_name, self._main_meters[0]),
                StripReading("output", 2, "", self._main_meters[1]),
                StripReading("output", 3, "", self._main_meters[2]),
            ],
        )

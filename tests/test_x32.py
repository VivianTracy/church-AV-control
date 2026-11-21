import struct

from church_av.a2ui import build_messages
from church_av.agent import _facts
from church_av.detector import Detector
from church_av.models import BoardReading, InputReading, StripReading
from church_av.oscutil import decode_message, encode_message, parse_meter_blob
from church_av.path import describe_path
from church_av.policy import decide_mapping
from church_av.runbook import BoardChannel, load_runbook, write_strip
from church_av.server import Booth
from church_av.x32_read import X32Reader, linear_to_db, usable_name
from pathlib import Path

from test_booth import _settings
from test_detector import runbook, snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_meter_blob_matches_the_x32_layout():
    raw = bytes.fromhex(
        "2f6d65746572732f360000002c6200000000001404000000"
        "fd1d2137fdff7f3f0000803f6ebbd534"
    )
    address, args = decode_message(raw)
    assert address == "/meters/6"
    values = parse_meter_blob(args[0])
    assert len(values) == 4
    assert values[2] == 1.0
    assert linear_to_db(1.0) == 0.0
    assert linear_to_db(0.0) == -100.0


def test_a_factory_scribble_is_not_a_name():
    assert usable_name("Ch20", 20) == ""
    assert usable_name("Ch 01", 1) == ""
    assert usable_name("Pulpit", 20) == "Pulpit"
    assert usable_name("Keys L", 10) == "Keys L"


def test_empty_host_does_not_open_a_socket():
    reading = X32Reader("").read()
    assert reading.connected is False
    assert "X32_HOST" in (reading.error or "")


class _Link:
    def __init__(self, replies):
        self.replies = replies
        self.sent = []
        self._queue = []

    def send(self, packet: bytes) -> None:
        self.sent.append(packet)
        address, args = decode_message(packet)
        reply = self.replies(address, args)
        if reply is not None:
            self._queue.append(reply)

    def recv(self, timeout: float) -> bytes | None:
        del timeout
        if not self._queue:
            return None
        return self._queue.pop(0)

    def close(self) -> None:
        return None


def _meter_packet(address: str, values: list[float]) -> bytes:
    blob = struct.pack("<i", len(values)) + struct.pack("<" + "f" * len(values), *values)
    return encode_message(address, [blob])


def test_reader_collects_meters_and_never_writes():
    def replies(address, args):
        if address == "/meters" and args and args[0] == "/meters/0":
            values = [0.0] * 70
            values[19] = 1.0
            values[48] = 0.5
            return _meter_packet("/meters/0", values)
        if address == "/meters" and args and args[0] == "/meters/2":
            values = [0.0] * 49
            values[22] = 0.5
            values[23] = 0.5
            return _meter_packet("/meters/2", values)
        if address == "/ch/01/config/name":
            return encode_message(address, ["Ch01"])
        if address == "/ch/20/config/name":
            return encode_message(address, ["Pulpit"])
        if address == "/ch/20/mix/on":
            return encode_message(address, [1])
        return None

    link = _Link(replies)
    reading = X32Reader(transport=link).read()
    assert reading.connected is True
    pulpit = reading.channels[19]
    assert pulpit.name == "Pulpit"
    assert pulpit.meter_db == 0.0
    assert pulpit.on is True
    assert reading.channels[0].name == ""
    assert reading.buses[0].meter_db == linear_to_db(0.5)
    assert reading.main[0].meter_db == linear_to_db(0.5)
    for packet in link.sent:
        address, args = decode_message(packet)
        assert not address.endswith("/mix/fader")
        if address.endswith("/config/name") or address.endswith("/mix/on"):
            assert args == []
    assert not any(name.startswith("set_") for name in dir(X32Reader) if not name.startswith("__"))


def test_a_live_console_name_replaces_the_photo_label():
    book = load_runbook(ROOT / "runbook" / "church.yaml").with_strip("pulpit", "14")
    quiet = BoardReading(
        connected=True,
        channels=[StripReading("channel", 14, "", meter_db=-12)],
    )
    assert book.resolved_name("pulpit", quiet) == "Moderator"
    renamed = BoardReading(
        connected=True,
        channels=[StripReading("channel", 14, "Host", meter_db=-12)],
    )
    assert book.resolved_name("pulpit", renamed) == "Host"
    assert book.resolved_name("handheld") is None


def _sunday():
    return runbook(
        expect_stream_live=True,
        usb_source="main",
        channels=[
            BoardChannel("pulpit", "Pulpit mic", "Pulpit", "channel", "20"),
            BoardChannel("handheld", "Handheld mic"),
            BoardChannel("main", "Main mix", "", "main"),
        ],
    )


def _board(pulpit: float, main: float, bus: float | None = None) -> BoardReading:
    channels = [
        StripReading("channel", index, "Pulpit" if index == 20 else "", pulpit if index == 20 else -100.0, True)
        for index in range(1, 33)
    ]
    buses = [
        StripReading("bus", index, "", None if bus is None else (bus if index == 5 else -100.0))
        for index in range(1, 17)
    ]
    mains = [StripReading("output", 1, "", main), StripReading("output", 2, "", main)]
    return BoardReading(connected=True, channels=channels, buses=buses, main=mains)


def _silent_obs(**overrides):
    base = dict(
        streaming=True,
        inputs=[InputReading(name="Audio Input Capture", muted=False, volume_db=-1.8, meter_db=-80)],
    )
    base.update(overrides)
    return snapshot(**base)


def _silence(reading, book):
    detector = Detector()
    detector.detect(reading, book)
    detector.detect(reading, book)
    return {fault.id for fault in detector.detect(reading, book)}


def test_a_quiet_mic_is_not_an_obs_failure():
    reading = _silent_obs()
    reading.board = _board(pulpit=-80, main=-80)
    assert "path_mic_dark" in _silence(reading, _sunday())


def test_a_hot_usb_send_with_a_quiet_obs_input_is_the_obs_side():
    reading = _silent_obs()
    reading.board = _board(pulpit=-12, main=-8)
    faults = _silence(reading, _sunday())
    assert "path_obs_dark" in faults
    assert "path_mic_dark" not in faults


def test_a_quiet_stream_bus_is_its_own_stage():
    reading = _silent_obs()
    reading.board = _board(pulpit=-12, main=-8, bus=-80)
    book = _sunday()
    book = runbook(
        expect_stream_live=True,
        usb_source="main",
        channels=book.channels,
        stream_mix=BoardChannel("stream", "Stream mix", "", "bus", "5"),
    )
    assert "path_stream_dark" in _silence(reading, book)


def test_a_quiet_mic_is_normal_while_obs_still_has_level():
    reading = snapshot()
    reading.board = _board(pulpit=-80, main=-12)
    report = describe_path(reading, _sunday())
    assert report.fault_id == ""
    assert "reaching OBS" in report.conclusion
    assert "isn't reaching" not in report.conclusion


def test_the_diagram_shows_the_stages_when_osc_is_connected():
    reading = snapshot()
    reading.board = _board(pulpit=-12, main=-10)
    surface = build_messages(reading, _sunday(), [], allow_apply=False)[0]["createSurface"]
    diagram = next(item for item in surface["components"] if item["id"] == "setup_diagram")
    boxes = [box for stage in diagram["lanes"][0]["stages"] for box in stage]
    pulpit = next(box for box in boxes if "Pulpit" in box["title"])
    usb = next(box for box in boxes if box["id"] == "usb")
    obs = next(box for box in boxes if box["id"] == "obs")
    assert "-12" in pulpit["detail"]
    assert "-10" in usb["detail"]
    assert obs["status"] == "Open"
    assert "-18" in obs["detail"]


def test_operator_mapping_writes_the_runbook_and_not_the_mixer(tmp_path):
    path = tmp_path / "church.yaml"
    path.write_text((ROOT / "runbook" / "church.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    book = load_runbook(path)
    refused = decide_mapping("main", "1", book)
    assert refused.allowed is False
    assert decide_mapping("handheld", "99", book).allowed is False
    allowed = decide_mapping("handheld", "14", book)
    assert allowed.allowed is True
    reader = type("Reader", (), {"calls": [], "read": lambda self: snapshot(), "close": lambda self: None})()
    booth = Booth(_settings(False, path), reader, book)
    result = booth.handle_action(
        {"version": "1.0", "action": {"name": "map_channel", "context": {"channelId": "handheld", "strip": "14"}}}
    )
    assert result["ok"] is True
    assert reader.calls == []
    assert booth.runbook.channel("handheld").strip == "14"
    assert booth.runbook.resolved_name("handheld") == "Moderator"
    saved = path.read_text(encoding="utf-8")
    assert 'strip: "14"' in saved
    assert 'strip: "20"' not in saved
    write_strip(path, "stream", "3")
    assert 'strip: "3"' in path.read_text(encoding="utf-8")


def test_facts_keep_the_usb_feed_and_the_osc_mixer_apart():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    text = _facts(snapshot(), book, [])
    assert "one combined X-USB feed" in text
    assert "separately over OSC" in text
    assert "1-9 Worship" in text
    assert "14 Moderator" in text
    assert "15 Reception" in text
    assert "20 Pulpit" not in text
    assert "ADJ PC-100A" in text
    assert "No dedicated stream bus" in text

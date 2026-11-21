import json

from church_av.a2ui import CATALOG_ID, build_messages
from church_av.detector import Detector
from church_av.models import Action, InputReading, Snapshot
from church_av.runbook import load_runbook
from church_av.server import Booth
from pathlib import Path

from test_detector import runbook, snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_runbook_matches_the_service_machine():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    assert book.house_mix.obs_name == "Audio Input Capture"
    assert book.house_mix.windows_device == "Microphone (X-USB)"
    assert book.scene_note("2-Slide Only").picture.startswith("Slide PC")
    assert book.expect_stream_live is False


def test_a2ui_message_uses_the_basic_catalog():
    messages = build_messages(snapshot(), runbook(), [], allow_apply=False)
    surface = messages[0]["createSurface"]
    assert surface["catalogId"] == CATALOG_ID
    assert surface["surfaceId"] == "booth"
    ids = [item["id"] for item in surface["components"]]
    assert ids[0] == "root"
    assert "2-Slide Only" in json.dumps(messages)


def test_apply_button_appears_only_when_apply_is_allowed():
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=True, volume_db=-1.8)]
    )
    faults = Detector().detect(reading, runbook())
    watching = json.dumps(build_messages(reading, runbook(), faults, allow_apply=False))
    armed = build_messages(reading, runbook(), faults, allow_apply=True)
    assert "apply_fix" not in watching
    button = next(item for item in armed[0]["createSurface"]["components"] if item["component"] == "Button")
    assert button["action"]["event"]["name"] == "apply_fix"
    assert button["action"]["event"]["context"]["faultId"] == "house_mix_muted"


class FakeReader:
    def __init__(self):
        self.muted = True
        self.calls = []

    def read(self) -> Snapshot:
        return snapshot(
            inputs=[InputReading(name="Audio Input Capture", muted=self.muted, volume_db=-1.8)]
        )

    def set_input_mute(self, name, muted):
        self.calls.append((name, muted))
        self.muted = muted

    def close(self):
        return None


def _settings(allow_apply: bool):
    from church_av.config import Settings

    return Settings(
        obs_host="10.1.10.167",
        obs_port=4455,
        obs_password="secret",
        allow_apply=allow_apply,
        booth_host="127.0.0.1",
        booth_port=8765,
        poll_seconds=2,
        runbook_path=ROOT / "runbook" / "church.yaml",
        root=ROOT,
    )


def test_apply_is_refused_while_watching_only():
    reader = FakeReader()
    booth = Booth(_settings(False), reader, runbook())
    booth.tick()
    result = booth.handle_action(
        {"version": "1.0", "action": {"name": "apply_fix", "context": {"faultId": "house_mix_muted"}}}
    )
    assert result["ok"] is False
    assert reader.calls == []


def test_apply_unmutes_only_the_house_mix():
    reader = FakeReader()
    booth = Booth(_settings(True), reader, runbook())
    booth.tick()
    result = booth.handle_action(
        {"version": "1.0", "action": {"name": "apply_fix", "context": {"faultId": "house_mix_muted"}}}
    )
    assert result["ok"] is True
    assert reader.calls == [("Audio Input Capture", False)]
    assert booth.faults == []


def test_apply_refuses_to_start_the_stream():
    reader = FakeReader()
    booth = Booth(_settings(True), reader, runbook())
    booth.tick()
    booth.faults[0].action = Action(kind="start_stream", summary="Start streaming")
    result = booth.handle_action(
        {"version": "1.0", "action": {"name": "apply_fix", "context": {"faultId": "house_mix_muted"}}}
    )
    assert result["ok"] is False
    assert "service PC" in result["message"]
    assert reader.calls == []

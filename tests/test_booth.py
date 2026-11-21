import json

from church_av.a2ui import CATALOG_ID, build_messages
from church_av.detector import Detector
from church_av.models import Action, InputReading, Snapshot
from church_av.runbook import load_runbook
from church_av.server import Booth
from pathlib import Path

from test_detector import runbook, snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_board_channels_are_unnamed_until_someone_reads_the_mixer():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    assert book.board_name == "Behringer X32"
    assert [item.id for item in book.channels] == ["pulpit", "handheld", "playback", "main"]
    assert all(item.name == "" for item in book.channels)
    question = book.board_instruction("main", "Raise {name} on the {board}.")
    assert question.startswith("What is the Main mix called on the Behringer X32?")
    assert "1" not in question


def test_a_named_board_channel_is_used_as_written():
    from church_av.runbook import BoardChannel

    book = runbook(
        channels=[BoardChannel(id="main", role="Main mix", name="Main L/R")],
    )
    step = book.board_instruction("main", "On the {board}, lower {name} a little.")
    assert step == "On the Behringer X32, lower Main L/R a little."


def test_clipping_asks_which_board_knob():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    loud = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=False, volume_db=-2, meter_db=-0.4)]
    )
    detector = Detector()
    detector.detect(loud, book)
    faults = detector.detect(loud, book)
    surface = build_messages(loud, book, faults, allow_apply=False)[0]["createSurface"]
    by_id = {item["id"]: item for item in surface["components"]}
    assert by_id["fault_0_place"]["text"] == "Behringer X32"
    assert by_id["fault_0_control"]["text"].startswith("What is the Main mix called")
    diagram = by_id["fault_0_board"]
    assert diagram["component"] == "X32Diagram"
    assert diagram["board"] == "Behringer X32"
    assert diagram["highlight"] == "main"
    assert diagram["direction"] == "down"
    assert [item["id"] for item in diagram["channels"]] == ["pulpit", "handheld", "playback", "main"]
    assert "fault_0_badge" not in by_id
    assert "fault_0_fader" not in by_id


def test_runbook_matches_the_service_machine():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    assert book.house_mix.obs_name == "Audio Input Capture"
    assert book.house_mix.windows_device == "Microphone (X-USB)"
    assert book.scene_note("2-Slide Only").picture.startswith("Slide PC")
    assert book.expect_stream_live is False


def test_graphic_surface_shows_the_stage_and_the_mixer_control():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=True, volume_db=-1.8)]
    )
    faults = Detector().detect(reading, runbook())
    surface = build_messages(reading, book, faults, allow_apply=False)[0]["createSurface"]
    by_id = {item["id"]: item for item in surface["components"]}
    assert by_id["stage"]["component"] == "Row"
    assert by_id["pane_0_label"]["text"] == "Slide PC"
    assert by_id["fault_0_place"]["text"] == "OBS Audio Mixer"
    assert by_id["fault_0_control"]["text"] == "Audio Input Capture"
    assert by_id["fault_0_icon"]["name"] == "volumeMute"
    assert by_id["fault_0_fader"]["component"] == "Column"
    assert "meterDb" in surface["dataModel"]


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


def test_the_surface_has_a_chat_field_and_a_guide_card():
    surface = build_messages(snapshot(), runbook(), [], allow_apply=False)[0]["createSurface"]
    by_id = {item["id"]: item for item in surface["components"]}
    assert by_id["question"]["component"] == "TextField"
    assert by_id["ask"]["action"]["event"]["name"] == "ask"
    assert by_id["guide_reply"]["text"] == "Ask about what you see."


def test_ask_about_a_muted_mix_shows_the_speaker_steps():
    from church_av.server import _preview_state

    reader = FakeReader()
    booth = Booth(_settings(False), reader, runbook())
    result = booth.handle_action(
        {
            "version": "1.0",
            "action": {
                "name": "ask",
                "context": {
                    "question": "The mix is muted. What do I do?",
                    "preview": "muted_house_mix",
                },
            },
        }
    )
    assert result["ok"] is True
    assert reader.calls == []
    assert "speaker" in result["message"]
    assert "Audio Input Capture" in result["message"]
    text = json.dumps(_preview_state(booth, "muted_house_mix")["a2ui"])
    assert "You asked: The mix is muted. What do I do?" in text
    assert "volumeMute" in text
    assert "OBS Audio Mixer" in text


def test_an_empty_question_is_refused():
    booth = Booth(_settings(False), FakeReader(), runbook())
    result = booth.handle_action(
        {"version": "1.0", "action": {"name": "ask", "context": {"question": "  "}}}
    )
    assert result["ok"] is False
    assert booth.chat_guide is None


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

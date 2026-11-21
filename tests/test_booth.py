import json

from church_av.a2ui import CATALOG_ID, SceneHistory, build_messages
from church_av.detector import Detector
from church_av.models import Action, BoardReading, InputReading, Snapshot, StripReading
from church_av.runbook import load_runbook
from church_av.server import Booth
from pathlib import Path

from test_detector import runbook, snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_scribble_names_from_the_console_are_kept_and_the_rest_stay_unmapped():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    assert book.board_name == "Behringer X32"
    assert [item.id for item in book.channels] == ["pulpit", "handheld", "playback", "main"]
    assert book.channel("pulpit").name == ""
    assert book.channel("pulpit").strip == ""
    pulpit = book.board_instruction("pulpit", "Raise {name} on the {board}.")
    assert pulpit.startswith("What is the Pulpit mic called on the Behringer X32?")
    assert book.channel("handheld").name == ""
    assert book.channel("handheld").strip == ""
    assert book.channel("playback").strip == ""
    assert book.channel("main").name == ""
    question = book.board_instruction("main", "Raise {name} on the {board}.")
    assert question.startswith("What is the Main mix called on the Behringer X32?")
    assert "1" not in question
    worship = [item.strip for item in book.labels if item.name == "Worship"]
    assert worship == list(range(1, 10))
    names = {item.strip: item.name for item in book.labels}
    assert names[10] == "Keys L"
    assert names[11] == "Piano L"
    assert names[12] == "Piano R"
    assert names[13] == "Offering"
    assert names[14] == "Moderator"
    assert names[15] == "Reception"
    assert names[16] == "Speaker"
    assert 20 not in names
    assert book.stream_mix is not None
    assert book.stream_mix.strip == ""
    assert book.usb_source == "main"
    assert book.house_mix.windows_device == "Microphone (X-USB)"
    assert any(item.name == "ADJ PC-100A" for item in book.rack)


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


def test_graphic_surface_shows_the_scene_and_the_mixer_control():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=True, volume_db=-1.8)]
    )
    faults = Detector().detect(reading, runbook())
    surface = build_messages(reading, book, faults, allow_apply=False)[0]["createSurface"]
    by_id = {item["id"]: item for item in surface["components"]}
    assert "stage" not in by_id
    assert "pane_0_label" not in by_id
    assert "scene_card" not in by_id
    diagram = by_id["setup_diagram"]
    scene = next(box for stage in diagram["lanes"][1]["stages"] for box in stage if box["id"] == "scene")
    audio = [box for stage in diagram["lanes"][0]["stages"] for box in stage]
    obs = next(box for box in audio if box["id"] == "obs")
    youtube = next(box for box in audio if box["id"] == "youtube")
    assert scene["kicker"] == "On screen"
    assert scene["title"] == "2-Slide Only"
    assert scene["detail"].startswith("Slide PC")
    assert obs["title"] == "Audio Mixer"
    assert obs["status"] == "Muted"
    assert youtube["status"] == "Stopped"
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


def _settings(allow_apply: bool, runbook_path: Path | None = None):
    from church_av.config import Settings

    return Settings(
        obs_host="10.1.10.167",
        obs_port=4455,
        obs_password="secret",
        allow_apply=allow_apply,
        booth_host="127.0.0.1",
        booth_port=8765,
        poll_seconds=2,
        runbook_path=runbook_path or ROOT / "runbook" / "church.yaml",
        root=ROOT,
    )


def _channel(index: int, name: str, meter: float, on: bool | None) -> StripReading:
    return StripReading("channel", index, name, meter, on)


def test_path_box_lists_only_channels_reaching_the_house_mix():
    book = load_runbook(ROOT / "runbook" / "church.yaml")
    waiting = snapshot(program_scene="3-Stage 50/Slide 50", streaming=True)
    waiting_surface = build_messages(waiting, book, [], allow_apply=False)[0]["createSurface"]
    waiting_diagram = {item["id"]: item for item in waiting_surface["components"]}["setup_diagram"]
    waiting_audio = [box["title"] for stage in waiting_diagram["lanes"][0]["stages"] for box in stage]
    assert waiting_audio[0] == "Channels"
    assert "1-9 Worship" not in waiting_audio
    assert "14 Moderator" not in waiting_audio
    assert "Main L/R" in waiting_audio

    board = BoardReading(
        connected=True,
        channels=[
            _channel(2, "Worship", -18, True),
            _channel(3, "Worship", -80, True),
            _channel(14, "Moderator", -12, False),
            _channel(15, "Reception", -20, True),
            _channel(20, "", -10, True),
        ],
    )
    reading = snapshot(program_scene="3-Stage 50/Slide 50", streaming=True, board=board)
    surface = build_messages(reading, book, [], allow_apply=False)[0]["createSurface"]
    by_id = {item["id"]: item for item in surface["components"]}
    diagram = by_id["setup_diagram"]
    assert by_id["path_card"]["component"] == "Card"
    audio = [box["title"] for stage in diagram["lanes"][0]["stages"] for box in stage]
    picture = [box["title"] for stage in diagram["lanes"][1]["stages"] for box in stage]
    assert audio[:3] == ["2 Worship", "15 Reception", "Channel 20"]
    assert "3 Worship" not in audio
    assert "14 Moderator" not in audio
    assert "Main L/R" in audio
    assert "X-USB" in audio
    assert "Audio Mixer" in audio
    assert "YouTube" in audio
    assert "Wall Cam" in picture
    assert "Slide PC" in picture
    chosen = next(box for stage in diagram["lanes"][0]["stages"] for box in stage if box["id"] == "ch-2")
    assert chosen["note"] == "2 Worship is open at -18 dB and is reaching the Audio Mixer."
    assert "left off" in " ".join(diagram["aside"])


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


def test_scene_chips_are_the_current_scene_and_the_two_used_before_it():
    history = SceneHistory()
    history.note("1-Stage Only")
    history.note("2-Slide Only")
    history.note("3-Stage 50/Slide 50")
    history.note("4-Stage 100/Slide 30")
    assert history.names() == ["4-Stage 100/Slide 30", "3-Stage 50/Slide 50", "2-Slide Only"]

    reading = snapshot(
        program_scene="4-Stage 100/Slide 30",
        scenes=["1-Stage Only", "2-Slide Only", "3-Stage 50/Slide 50", "4-Stage 100/Slide 30"],
    )
    surface = build_messages(reading, runbook(), [], allow_apply=False, recent_scenes=history.names())[0][
        "createSurface"
    ]
    by_id = {item["id"]: item for item in surface["components"]}
    assert by_id["scene_strip"]["children"] == ["scene_current", "scene_1", "scene_2"]
    assert by_id["scene_current_label"]["text"] == "4-Stage 100/Slide 30"
    assert by_id["scene_1_label"]["text"] == "3-Stage 50/Slide 50"
    assert by_id["scene_2_label"]["text"] == "2-Slide Only"
    assert by_id["scene_1"]["action"]["event"]["name"] == "set_scene"
    assert by_id["scene_1"]["action"]["event"]["context"]["sceneName"] == "3-Stage 50/Slide 50"
    labels = [item["text"] for item in by_id.values() if item["id"].endswith("_label") and item["id"].startswith("scene_")]
    assert labels == ["4-Stage 100/Slide 30", "3-Stage 50/Slide 50", "2-Slide Only"]
    assert "scene_3" not in by_id


class SceneReader:
    def __init__(self):
        self.scene = "2-Slide Only"
        self.calls = []

    def read(self) -> Snapshot:
        return snapshot(program_scene=self.scene, scenes=["2-Slide Only", "1-Stage Only"])

    def set_program_scene(self, name):
        self.calls.append(name)
        self.scene = name

    def close(self):
        return None


def test_clicking_a_scene_switches_the_program_scene():
    reader = SceneReader()
    booth = Booth(_settings(False), reader, runbook())
    booth.tick()
    assert booth.scenes.names() == ["2-Slide Only"]
    same = booth.handle_action(
        {"version": "1.0", "action": {"name": "set_scene", "context": {"sceneName": "2-Slide Only"}}}
    )
    assert same["ok"] is True
    assert reader.calls == []

    switched = booth.handle_action(
        {"version": "1.0", "action": {"name": "set_scene", "context": {"sceneName": "1-Stage Only"}}}
    )
    assert switched["ok"] is True
    assert reader.calls == ["1-Stage Only"]
    assert booth.snapshot.program_scene == "1-Stage Only"
    assert booth.scenes.names() == ["1-Stage Only", "2-Slide Only"]
    chips = {item["id"]: item for item in booth.state()["a2ui"][0]["createSurface"]["components"]}
    assert chips["scene_strip"]["children"] == ["scene_current", "scene_1"]
    assert chips["scene_current_label"]["text"] == "1-Stage Only"
    assert chips["scene_1_label"]["text"] == "2-Slide Only"

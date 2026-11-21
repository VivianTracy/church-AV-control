from church_av.fixtures import load_fixture
from church_av.models import Action
from church_av.policy import decide
from church_av.runbook import load_runbook
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def book():
    return load_runbook(ROOT / "runbook" / "church.yaml")


def test_x32_fader_move_is_refused():
    snapshot = load_fixture("healthy")[0]
    decision = decide(
        Action(kind="x32_set_fader", summary="Raise pulpit", input_name="20"),
        snapshot,
        book(),
    )
    assert decision.allowed is False
    assert decision.action is None
    assert "OSC" in decision.reason


def test_stream_start_is_refused():
    snapshot = load_fixture("healthy")[0]
    decision = decide(Action(kind="start_stream", summary="Start streaming"), snapshot, book())
    assert decision.allowed is False
    assert decision.action is None
    assert "service PC" in decision.reason


def test_house_mix_mute_is_refused():
    snapshot = load_fixture("healthy")[0]
    decision = decide(
        Action(
            kind="set_input_mute",
            summary="Mute the house mix",
            input_name="Audio Input Capture",
            muted=True,
        ),
        snapshot,
        book(),
    )
    assert decision.allowed is False
    assert "Muting the Audio Mixer" in decision.reason


def test_fader_above_zero_is_refused():
    snapshot = load_fixture("healthy")[0]
    decision = decide(
        Action(
            kind="set_input_volume",
            summary="Raise the house mix",
            input_name="Audio Input Capture",
            volume_db=2.0,
        ),
        snapshot,
        book(),
    )
    assert decision.allowed is False
    assert "above 0 dB" in decision.reason


def test_unmute_house_mix_is_allowed():
    snapshot = load_fixture("muted_house_mix")[0]
    decision = decide(
        Action(
            kind="set_input_mute",
            summary="Unmute Audio Input Capture",
            input_name="Audio Input Capture",
            muted=False,
        ),
        snapshot,
        book(),
    )
    assert decision.allowed is True
    assert decision.action is not None
    assert decision.action.muted is False


def test_camera_unmute_is_refused():
    snapshot = load_fixture("healthy")[0]
    decision = decide(
        Action(kind="set_input_mute", summary="Unmute Wall Cam", input_name="Wall Cam", muted=False),
        snapshot,
        book(),
    )
    assert decision.allowed is False


def test_large_fader_request_is_one_step_and_stays_at_or_below_zero():
    snapshot = load_fixture("low_fader")[0]
    decision = decide(
        Action(
            kind="set_input_volume",
            summary="Restore the house mix",
            input_name="Audio Input Capture",
            volume_db=-2.0,
        ),
        snapshot,
        book(),
    )
    assert decision.allowed is True
    assert decision.action is not None
    assert decision.action.volume_db == -32.0


def test_scene_change_needs_confirmation_and_a_real_scene():
    snapshot = load_fixture("healthy")[0]
    unconfirmed = decide(
        Action(kind="set_program_scene", summary="Slides", scene_name="2-Slide Only"),
        snapshot,
        book(),
    )
    assert unconfirmed.allowed is False

    missing = decide(
        Action(kind="set_program_scene", summary="Missing", scene_name="Lobby"),
        snapshot,
        book(),
        confirmed=True,
    )
    assert missing.allowed is False

    confirmed = decide(
        Action(kind="set_program_scene", summary="Slides", scene_name="2-Slide Only"),
        snapshot,
        book(),
        confirmed=True,
    )
    assert confirmed.allowed is True
    assert confirmed.action is not None
    assert confirmed.action.scene_name == "2-Slide Only"

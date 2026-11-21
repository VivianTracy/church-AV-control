import pytest

from church_av.agent import INSTRUCTION, build_agent, build_tools, explain
from church_av.detector import Detector
from church_av.fixtures import load_fixture
from church_av.runbook import load_runbook
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _book():
    return load_runbook(ROOT / "runbook" / "church.yaml")


def _faults(name: str):
    snapshot = load_fixture(name)[0]
    return snapshot, Detector().detect(snapshot, _book())


def test_tools_only_read_the_rehearsal():
    snapshot, faults = _faults("muted_house_mix")
    tools = build_tools(snapshot, _book(), faults)
    assert [tool.__name__ for tool in tools] == [
        "get_snapshot",
        "get_runbook",
        "list_open_faults",
    ]
    assert tools[0]()["snapshot"]["program_scene"] == "1-Stage Only"
    assert tools[1]()["runbook"]["house_mix"]["obs_name"] == "Audio Input Capture"
    assert tools[2]()["faults"][0]["id"] == "house_mix_muted"
    for tool in tools:
        assert "does not change OBS" in tool.__doc__


def test_missing_key_uses_detector_text_and_skips_the_model():
    snapshot, faults = _faults("muted_house_mix")
    called = False

    def completer(prompt, tools):
        nonlocal called
        called = True
        return "from the model"

    guide = explain(snapshot, _book(), faults, api_key="", completer=completer)
    assert called is False
    assert guide.source == "detector"
    assert "House mix is muted" in guide.reply
    assert "speaker" in guide.reply
    assert "Audio Input Capture" in guide.reply


def test_a_key_uses_the_model_reply():
    snapshot, faults = _faults("muted_house_mix")

    def completer(prompt, tools):
        assert "where to adjust" in prompt
        assert len(tools) == 3
        return "Click the speaker under Audio Input Capture."

    guide = explain(snapshot, _book(), faults, api_key="test-key", completer=completer)
    assert guide.source == "adk"
    assert guide.reply.startswith("Click the speaker")


def test_a_failed_model_call_falls_back_to_the_detector():
    snapshot, faults = _faults("healthy")

    def completer(prompt, tools):
        raise RuntimeError("network down")

    guide = explain(snapshot, _book(), faults, api_key="test-key", completer=completer)
    assert guide.source == "detector"
    assert "house mix is open" in guide.reply


def test_a_health_check_is_answered_when_obs_is_down():
    from church_av.models import Snapshot

    guide = explain(
        Snapshot(connected=False, error="timed out"),
        _book(),
        [],
        question="suggest steps to do a health check before streaming starts.",
        api_key="",
    )
    assert guide.source == "detector"
    assert "Audio Input Capture" in guide.reply
    assert "Wall Cam" in guide.reply
    assert "Cannot read the service machine" not in guide.reply


def test_an_improvement_question_is_answered_in_chinese_when_obs_is_down():
    from church_av.models import Snapshot

    snapshot = Snapshot(connected=False, error="timed out")
    faults = Detector().detect(snapshot, _book())

    called = False

    def completer(prompt, tools):
        nonlocal called
        called = True
        raise RuntimeError("503")

    guide = explain(
        snapshot,
        _book(),
        faults,
        question="suggest ways to improve streaming? answer in Chinese",
        api_key="test-key",
        completer=completer,
    )
    assert called is False
    assert guide.source == "detector"
    assert "服务电脑" in guide.reply
    assert "Audio Input Capture" in guide.reply
    assert "Behringer X32" in guide.reply
    assert "Cannot read the service machine" not in guide.reply


def test_instruction_keeps_the_agent_from_driving_the_stream():
    assert "Do not start or stop the stream" in INSTRUCTION
    assert "You cannot change OBS" in INSTRUCTION
    assert "above 0 dB" in INSTRUCTION
    assert "ask what the knob is called" in INSTRUCTION
    assert "If they ask for Chinese, write the whole reply in Chinese." in INSTRUCTION


def test_installed_agent_keeps_the_read_only_tools():
    pytest.importorskip("google.adk")
    snapshot, faults = _faults("healthy")
    agent = build_agent(build_tools(snapshot, _book(), faults))
    assert agent is not None
    names = [getattr(tool, "__name__", None) or getattr(tool, "name", None) for tool in agent.tools]
    assert names == ["get_snapshot", "get_runbook", "list_open_faults"]


def test_build_agent_is_absent_without_the_package(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("google.adk"):
            raise ImportError("blocked for the test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    snapshot, faults = _faults("healthy")
    assert build_agent(build_tools(snapshot, _book(), faults)) is None

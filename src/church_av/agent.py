"""Read-only booth agent. A missing Gemini key uses the detector's own words."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from church_av.models import Fault, Snapshot
from church_av.runbook import Runbook

MODEL = "gemini-3.5-flash"

INSTRUCTION = """
You are the church stream booth assistant.
Answer the question the operator asked.
The facts in the message are the only live readings and church names you have.
Do not recite a general checklist unless the operator asked for one.
Reply in two or three sentences a volunteer can follow from across the room.
Reply in the language the operator wrote in. An English question gets an English reply. If they ask for Chinese, write the whole reply in Chinese.
Name the OBS control, such as the Audio Mixer speaker for Audio Input Capture,
or the Behringer X32 channel when the runbook names it.
Do not invent a board channel name.
If a board channel name is empty, ask what the knob is called.
Do not start or stop the stream, and do not click Go Live.
Do not mute the house mix, and do not suggest a fader above 0 dB.
You cannot change OBS. The operator taps Apply for a change the policy allows.
""".strip()


@dataclass(frozen=True)
class Guide:
    reply: str
    source: str


def build_tools(snapshot: Snapshot, runbook: Runbook, faults: list[Fault]) -> list:
    """Tools the model may call. None of them write to OBS."""

    def get_snapshot() -> dict:
        """Return the latest OBS reading: scene, stream, and input levels.

        This does not change OBS.
        """
        return {"status": "success", "snapshot": asdict(snapshot)}

    def get_runbook() -> dict:
        """Return the church runbook: house mix, sources, and scenes.

        This does not change OBS.
        """
        return {"status": "success", "runbook": asdict(runbook)}

    def list_open_faults() -> dict:
        """Return the faults the detector currently has open.

        Do not invent a fault that is not in this list. This does not change OBS.
        """
        return {
            "status": "success",
            "faults": [asdict(fault) for fault in faults],
        }

    return [get_snapshot, get_runbook, list_open_faults]


def build_agent(tools: list):
    """Return an ADK agent, or None when the ADK package is not installed."""
    try:
        from google.adk.agents.llm_agent import Agent
    except ImportError:
        return None
    return Agent(
        name="church_booth",
        model=MODEL,
        description="Explains church stream readings and points to the control to adjust.",
        instruction=INSTRUCTION,
        tools=tools,
    )


def explain(
    snapshot: Snapshot,
    runbook: Runbook,
    faults: list[Fault],
    question: str | None = None,
    api_key: str = "",
    completer=None,
) -> Guide:
    """Explain the reading. Without an API key, or if the model call fails, use the detector text."""
    local = local_reply(snapshot, runbook, faults, question)
    if not api_key.strip():
        return Guide(local, "detector")
    tools = build_tools(snapshot, runbook, faults)
    try:
        if completer is not None:
            reply = completer(_prompt(question, snapshot), tools)
        else:
            reply = _ask_model(api_key, _prompt(question, snapshot), _facts(snapshot, runbook, faults))
    except Exception:
        if question:
            return Guide(_could_not_answer(question, snapshot), "detector")
        return Guide(local, "detector")
    reply = (reply or "").strip()
    if not reply:
        return Guide(local, "detector")
    return Guide(reply, "adk")


def local_reply(
    snapshot: Snapshot,
    runbook: Runbook,
    faults: list[Fault],
    question: str | None = None,
) -> str:
    if question and not snapshot.connected and _asks_for_a_check(question):
        return _health_check(snapshot, runbook, _wants_chinese(question))
    if not faults:
        return _calm_status(snapshot, runbook)
    blocks = []
    for fault in faults:
        steps = " ".join(fault.steps)
        blocks.append(f"{fault.title}. {fault.evidence} {steps}".strip())
    return " ".join(blocks)


def _calm_status(snapshot: Snapshot, runbook: Runbook) -> str:
    if not snapshot.connected:
        return snapshot.error or "Cannot read the service machine."
    scene = snapshot.program_scene or "No scene"
    note = runbook.scene_note(snapshot.program_scene)
    picture = f", {note.picture}" if note else ""
    stream = "live" if snapshot.streaming else "stopped"
    house = snapshot.input_named(runbook.house_mix.obs_name)
    if house is None:
        mix = f"{runbook.house_mix.obs_name} is missing"
    elif house.muted:
        mix = "the house mix is muted"
    else:
        mix = "the house mix is open"
    return f"On screen: {scene}{picture}. YouTube output is {stream}. {mix.capitalize()}."


def _asks_for_a_check(question: str) -> bool:
    text = question.lower()
    return any(phrase in text for phrase in ("health", "before streaming", "before the stream", "checklist", "开播前", "检查"))


def _wants_chinese(question: str) -> bool:
    if "中文" in question or "汉语" in question:
        return True
    if "chin" in question.lower():
        return True
    return any("\u4e00" <= char <= "\u9fff" for char in question)


def _could_not_answer(question: str, snapshot: Snapshot) -> str:
    if _wants_chinese(question):
        if not snapshot.connected:
            return "现在回答不了这个问题。服务电脑读不到，所以没有现场画面和音量。"
        return "现在回答不了这个问题。请再问一次。"
    if not snapshot.connected:
        return "The guide could not answer that question. The service PC cannot be read from here, so there is no live picture or level."
    return "The guide could not answer that question. Ask again."


def _muted_names(runbook: Runbook) -> list[str]:
    return [source.obs_name for source in runbook.sources if "muted on purpose" in source.audio.lower()]


def _health_check(snapshot: Snapshot, runbook: Runbook, chinese: bool = False) -> str:
    mix = runbook.house_mix
    muted = _muted_names(runbook)
    if chinese:
        muted_text = "、".join(muted) if muted else "摄像头和幻灯片"
        where = "现在读不到服务电脑，请在那台电脑上做这些事。" if not snapshot.connected else ""
        return (
            f"{where}开播前确认 OBS 已打开，并且 WebSocket 服务器已启用。"
            f"在音频混音器里，{mix.obs_name} 保持取消静音，推子靠近 {mix.target_db:.0f} dB，{muted_text} 保持静音。"
            f"在 {runbook.board_name} 上把主输出和要用的话筒推起来。通道名称还没写入手册，请看调音台面板上的标签。"
            "开播请在服务电脑上点 Start Streaming。这个控制台不会替你开播。"
        )
    muted_text = ", ".join(muted) if muted else "the camera and slide sources"
    where = "The service PC cannot be read from here, so do this on that PC. " if not snapshot.connected else ""
    return (
        f"{where}Before Start Streaming, confirm OBS is open and the WebSocket server is enabled. "
        f"In the Audio Mixer, {mix.obs_name} stays unmuted with the fader near {mix.target_db:.0f} dB, "
        f"and {muted_text} stay muted. "
        f"On the {runbook.board_name}, bring up the main mix and the mics you need. "
        "Those channel names are not written down yet, so read the labels on the board. "
        "Start the stream on the service PC. This booth will not start it."
    )


def _prompt(question: str | None, snapshot: Snapshot) -> str:
    if question:
        asked = "The operator asked: " + question.strip() + " "
        language = "Reply in the language of the question. An English question gets an English reply. If they ask for Chinese, write the whole reply in Chinese. "
        if not snapshot.connected:
            return (
                asked
                + "Answer this question from the facts below. The live OBS reading is unavailable, so say that in one sentence and do not invent a scene or a meter. "
                + "Do not reply with a general pre-stream checklist unless they asked for one. "
                + language
            )
        return (
            asked
            + "Answer this question from the facts below. "
            + language
            + "Reply in two or three sentences and name where to adjust."
        )
    return (
        "A detector fault may be open. Use the tools. "
        "Reply in two or three sentences a volunteer can follow, and name where to adjust."
    )


def _facts(snapshot: Snapshot, runbook: Runbook, faults: list[Fault]) -> str:
    mix = runbook.house_mix
    parts = [
        f"House mix in OBS: {mix.obs_name}, Windows device {mix.windows_device}, target {mix.target_db:.0f} dB.",
        f"Mixer: {runbook.board_name}.",
    ]
    if snapshot.connected:
        scene = snapshot.program_scene or "unknown"
        stream = "live" if snapshot.streaming else "stopped"
        parts.append(f"OBS is connected. Program scene {scene}. YouTube output {stream}.")
        house = snapshot.input_named(mix.obs_name)
        if house is None:
            parts.append(f"{mix.obs_name} is missing from this reading.")
        else:
            state = "muted" if house.muted else "open"
            level = f"{house.volume_db:.0f} dB" if house.volume_db is not None else "an unknown fader"
            parts.append(f"{mix.obs_name} is {state} at {level}.")
    else:
        parts.append("OBS cannot be read. There is no live scene, meter, or YouTube status.")
    if faults:
        parts.append("Open faults: " + "; ".join(fault.title for fault in faults[:4]) + ".")
    else:
        parts.append("No open faults.")
    return " ".join(parts)


def _ask_model(api_key: str, prompt: str, facts: str) -> str:
    """One short model call. No tool round trip and no retry while Flash is busy."""
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=12000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{prompt}\nFacts: {facts}",
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCTION,
            max_output_tokens=220,
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        ),
    )
    return (response.text or "").strip()

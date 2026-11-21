"""Read-only booth agent. A missing Gemini key uses the detector's own words."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from church_av.models import Fault, Snapshot
from church_av.runbook import Runbook

INSTRUCTION = """
You are the church stream booth assistant.
Use get_snapshot, get_runbook, and list_open_faults before you answer.
Reply in two or three sentences a volunteer can follow from across the room.
Reply in the language the operator wrote in. If they ask for Chinese, write the whole reply in Chinese.
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
        model="gemini-3.5-flash",
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
    if not api_key.strip() or _answer_from_the_runbook(question, snapshot):
        return Guide(local, "detector")
    tools = build_tools(snapshot, runbook, faults)
    try:
        if completer is not None:
            reply = completer(_prompt(question, snapshot), tools)
        else:
            reply = _ask_adk(tools, _prompt(question, snapshot))
    except Exception:
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
    chinese = bool(question) and _wants_chinese(question)
    if question and _asks_for_a_check(question):
        return _health_check(snapshot, runbook, chinese)
    if question and (_asks_to_improve(question) or (chinese and not snapshot.connected)):
        return _improve_streaming(snapshot, runbook, chinese)
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


def _asks_to_improve(question: str) -> bool:
    text = question.lower()
    return any(phrase in text for phrase in ("improve", "better sound", "better picture", "优化", "改善", "改进"))


def _wants_chinese(question: str) -> bool:
    if "中文" in question or "汉语" in question:
        return True
    if "chin" in question.lower():
        return True
    return any("\u4e00" <= char <= "\u9fff" for char in question)


def _answer_from_the_runbook(question: str | None, snapshot: Snapshot) -> bool:
    if not question or snapshot.connected:
        return False
    return _asks_for_a_check(question) or _asks_to_improve(question) or _wants_chinese(question)


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


def _improve_streaming(snapshot: Snapshot, runbook: Runbook, chinese: bool) -> str:
    mix = runbook.house_mix
    muted = _muted_names(runbook)
    if chinese:
        muted_text = "、".join(muted) if muted else "摄像头和幻灯片"
        where = "现在读不到服务电脑，所以下面是在那台电脑上可以做的事，不是现场读数。" if not snapshot.connected else ""
        return (
            f"{where}声音：在 OBS 音频混音器里，让 {mix.obs_name} 保持取消静音，推子靠近 {mix.target_db:.0f} dB。"
            f"{muted_text} 保持静音，免得把现场声送回去造成回声。"
            f"在 {runbook.board_name} 上把主输出和要用的话筒推起来。通道名称还没写入手册，请看调音台面板上的标签。"
            "OBS 推子不要超过 0 dB。"
            "画面：如果 OBS 显示丢帧，先关掉服务电脑上别的程序。直播中不要改编码器、码率或分辨率，改了会中断播出。"
            "开播请在服务电脑上点 Start Streaming。这个控制台不会替你开播。"
        )
    muted_text = ", ".join(muted) if muted else "the camera and slide sources"
    where = (
        "The service PC cannot be read from here, so these are steps on that PC, not a live reading. "
        if not snapshot.connected
        else ""
    )
    return (
        f"{where}In the OBS Audio Mixer, leave {mix.obs_name} unmuted with the fader near {mix.target_db:.0f} dB, "
        f"and leave {muted_text} muted so they do not feed the room back as echo. "
        f"On the {runbook.board_name}, bring up the main mix and the mics you need, and read the labels on the board. "
        "Do not push the OBS fader above 0 dB. "
        "If OBS shows skipped frames, close other programs on the service PC. "
        "Leave the encoder, bitrate, and resolution alone during a broadcast, because changing them interrupts the stream. "
        "Start the stream on the service PC. This booth will not start it."
    )


def _prompt(question: str | None, snapshot: Snapshot) -> str:
    if question:
        asked = "The operator asked: " + question.strip() + " "
        language = "Reply in the language of the question. If they ask for Chinese, write the whole reply in Chinese. "
        if not snapshot.connected:
            return (
                asked
                + "OBS cannot be read right now. Answer from the runbook in two or three sentences. "
                + "Do not spend the reply on the disconnect error. "
                + language
            )
        return asked + "Use the tools. " + language + "Reply in two or three sentences and name where to adjust."
    return (
        "A detector fault may be open. Use the tools. "
        "Reply in two or three sentences a volunteer can follow, and name where to adjust."
    )


def _ask_adk(tools: list, prompt: str) -> str:
    agent = build_agent(tools)
    if agent is None:
        raise RuntimeError("google-adk is not installed")

    async def _run() -> str:
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        runner = InMemoryRunner(agent=agent, app_name="church_av")
        try:
            session = await runner.session_service.create_session(
                app_name=runner.app_name,
                user_id="booth",
            )
            message = types.Content(role="user", parts=[types.Part(text=prompt)])
            chunks: list[str] = []
            async for event in runner.run_async(
                user_id="booth",
                session_id=session.id,
                new_message=message,
            ):
                if hasattr(event, "is_final_response") and not event.is_final_response():
                    continue
                content = getattr(event, "content", None)
                if content is None:
                    continue
                for part in getattr(content, "parts", None) or []:
                    text = getattr(part, "text", None)
                    if text:
                        chunks.append(text)
            return "".join(chunks).strip()
        finally:
            close = getattr(runner, "close", None)
            if close is not None:
                await close()

    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=30))
    except TimeoutError as exc:
        raise RuntimeError("The guide took too long") from exc

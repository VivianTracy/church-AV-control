"""Read-only booth agent. A missing Gemini key uses the detector's own words."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from church_av.models import Fault, Snapshot
from church_av.path import describe_path, house_channels
from church_av.runbook import Runbook

MODEL = "gemini-3.5-flash"

INSTRUCTION = """
You are the church stream booth assistant.
Answer the question the operator asked.
The facts in the message are the only live readings and church names you have.
Do not recite a general checklist unless the operator asked for one.
Reply in the language the operator wrote in. An English question gets an English reply. A Chinese question gets a Chinese reply.
The path box draws the signal path. It lists only channels that are unmuted and above the noise floor. Explain that path. Do not draw it as text, and do not list quiet or muted channels as if they were in the house mix.
When no question was asked and you are explaining a detector fault, write the whole reply in Chinese.
OBS receives one combined X-USB feed, Audio Input Capture. Individual mics are not separate OBS inputs.
The Behringer X32 may be monitored separately over OSC. Those readings are channel, bus, and output meters.
Name the OBS control, such as the Audio Mixer speaker for Audio Input Capture,
or the Behringer X32 channel when the runbook or the console names it.
Do not invent a board channel name or a strip number.
If a board channel name is empty, ask what the knob is called.
If the strip is empty, ask the operator to enter the mapping.
When the OBS input is quiet, say which stage failed: the mic channel, the stream bus, the X32 USB output, or the OBS input.
A quiet mic is normal while the OBS meter is moving.
A bus or matrix can be a dedicated stream mix. Until one is assigned, YouTube uses the house mix, still as one X-USB input.
The ADJ PC-100A, amplifiers, wireless receivers, and lighting controller have no reading here.
Do not start or stop the stream, and do not click Go Live.
Do not mute the house mix, and do not suggest a fader above 0 dB.
You diagnose and guide. You cannot change OBS or the X32.
A deterministic policy decides which operator-approved changes are allowed.
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

        This does not change OBS. This does not change the X32.
        """
        return {"status": "success", "snapshot": asdict(snapshot)}

    def get_runbook() -> dict:
        """Return the church runbook: house mix, sources, and scenes.

        This does not change OBS. This does not change the X32.
        """
        return {"status": "success", "runbook": asdict(runbook)}

    def list_open_faults() -> dict:
        """Return the faults the detector currently has open.

        Do not invent a fault that is not in this list. This does not change OBS. This does not change the X32.
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
        return _health_check(snapshot, runbook, _question_is_chinese(question))
    if not question and faults:
        return _diagnosis(runbook, faults)
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


def _question_is_chinese(question: str) -> bool:
    if "中文" in question or "汉语" in question:
        return True
    if "in chinese" in question.lower():
        return True
    return any("\u4e00" <= char <= "\u9fff" for char in question)


def _could_not_answer(question: str, snapshot: Snapshot) -> str:
    if _question_is_chinese(question):
        if not snapshot.connected:
            return "现在回答不了这个问题。服务电脑读不到，所以没有现场画面和音量。"
        return "现在回答不了这个问题。请再问一次。"
    if not snapshot.connected:
        return "The guide could not answer that question. The service PC cannot be read from here, so there is no live picture or level."
    return "The guide could not answer that question. Ask again."


def _diagnosis(runbook: Runbook, faults: list[Fault]) -> str:
    return " ".join(_diagnosis_line(fault, runbook) for fault in faults)


def _diagnosis_line(fault: Fault, runbook: Runbook) -> str:
    mix = runbook.house_mix.obs_name
    board = runbook.board_name
    target = f"{runbook.house_mix.target_db:.0f}"
    lines = {
        "obs_disconnected": (
            "现在读不到服务电脑。请在那台电脑上打开 OBS，确认 WebSocket 服务器已启用，"
            "并且防火墙允许 OBS Studio 在专用网络上通信。"
        ),
        "house_mix_missing": f"OBS 里没有名为 {mix} 的输入。请找到 Microphone (X-USB)，或改手册里的名称。",
        "house_mix_muted": (
            f"房间混音被静音了。在 OBS 音频混音器里，点 {mix} 下面的喇叭，取消静音。"
            "Wall Cam、Mobile Cam、Slide PC 保持静音。"
        ),
        "house_mix_fader_low": (
            f"{mix} 的推子偏低。在 OBS 音频混音器里把它往上推一点，靠近 {target} dB。"
            f"如果调音台电平已经很低，先看 {board} 的主输出。"
        ),
        "house_mix_clipping": f"声音削波了。在 {board} 上把主输出拉低一点。OBS 里 {mix} 的推子保持不动。",
        "house_mix_silent": (
            f"房间混音没有声音。检查 {board} 的 USB 输出和主输出没有被静音，"
            f"并且 OBS 里 {mix} 保持取消静音。"
        ),
        "path_mic_dark": (
            f"话筒没有进到 {board}。先看已映射的话筒通道、推子和静音。这还不是 OBS 的问题。"
        ),
        "path_stream_dark": (
            f"话筒已经进了 {board}，但直播总线是静的。检查那路 bus 的发送和推子。"
            "OBS 里的 X-USB 仍然是一路总信号。"
        ),
        "path_usb_dark": (
            f"调音台前面的电平是有的，但 USB 输出是静的。检查 {board} 送到电脑的那路输出。"
        ),
        "path_obs_dark": (
            f"调音台的 USB 输出是有电平的，OBS 没有收到。看音频混音器里的 {mix}，"
            "确认没有静音，设备仍是 Microphone (X-USB)。"
        ),
        "stream_reconnecting": "直播正在重新连接。先等 OBS 自己连上，不要点停止。",
        "stream_stopped": "现在应该已经在直播，但 OBS 的输出是停的。请在服务电脑上点 Start Streaming。这个控制台不会替你开播。",
        "frames_skipped": "OBS 丢帧了。先关掉服务电脑上别的程序。直播中不要改编码器、码率或分辨率。",
    }
    return lines.get(fault.id, fault.title)


def _muted_names(runbook: Runbook) -> list[str]:
    return [source.obs_name for source in runbook.sources if "muted on purpose" in source.audio.lower()]


def _board_health(runbook: Runbook, chinese: bool = False) -> str:
    mapped = [
        f"{item.name} is channel {int(item.strip)}"
        for item in runbook.channels
        if item.kind == "channel" and item.known and item.strip.isdigit()
    ]
    unmapped = [item.role for item in runbook.channels if item.kind == "channel" and not item.strip]
    if chinese:
        mapped_text = f"{'，'.join(mapped)}。" if mapped else ""
        if unmapped:
            return f"{mapped_text}{'、'.join(unmapped)} 还没有对应通道，需要填写编号。"
        return mapped_text or "通道已经对应好了。"
    mapped_text = f"{', '.join(mapped)}. " if mapped else ""
    if unmapped:
        unmapped_text = f"{' and '.join(unmapped)} still need a strip number."
    else:
        unmapped_text = "Mapped channels are ready."
    return f"{mapped_text}{unmapped_text} Console scribbles are used when the channel is named."


def _scribble_fact(runbook: Runbook) -> str:
    labels = [item for item in runbook.labels if item.name.strip()]
    if not labels:
        return ""
    worship = [item.strip for item in labels if item.name == "Worship"]
    rest = [item for item in labels if item.name != "Worship"]
    parts: list[str] = []
    if len(worship) > 1 and worship == list(range(worship[0], worship[-1] + 1)):
        parts.append(f"{worship[0]}-{worship[-1]} Worship")
    else:
        parts.extend(f"{item.strip} {item.name}" for item in labels if item.name == "Worship")
    parts.extend(f"{item.strip} {item.name}" for item in rest)
    return "Console scribbles: " + ", ".join(parts) + "."


def _health_check(snapshot: Snapshot, runbook: Runbook, chinese: bool = False) -> str:
    mix = runbook.house_mix
    muted = _muted_names(runbook)
    if chinese:
        muted_text = "、".join(muted) if muted else "摄像头和幻灯片"
        where = "现在读不到服务电脑，请在那台电脑上做这些事。" if not snapshot.connected else ""
        return (
            f"{where}开播前确认 OBS 已打开，并且 WebSocket 服务器已启用。"
            f"在音频混音器里，{mix.obs_name} 保持取消静音，推子靠近 {mix.target_db:.0f} dB，{muted_text} 保持静音。"
            f"在 {runbook.board_name} 上把主输出和要用的话筒推起来。{_board_health(runbook, chinese=True)}"
            "开播请在服务电脑上点 Start Streaming。这个控制台不会替你开播。"
        )
    muted_text = ", ".join(muted) if muted else "the camera and slide sources"
    where = "The service PC cannot be read from here, so do this on that PC. " if not snapshot.connected else ""
    return (
        f"{where}Before Start Streaming, confirm OBS is open and the WebSocket server is enabled. "
        f"In the Audio Mixer, {mix.obs_name} stays unmuted with the fader near {mix.target_db:.0f} dB, "
        f"and {muted_text} stay muted. "
        f"On the {runbook.board_name}, bring up the main mix and the mics you need. "
        f"{_board_health(runbook)} "
        "Start the stream on the service PC. This booth will not start it."
    )


def _prompt(question: str | None, snapshot: Snapshot) -> str:
    if question:
        asked = "The operator asked: " + question.strip() + " "
        language = "Reply in the language of the question. An English question gets an English reply. A Chinese question gets a Chinese reply. "
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
            + "Name where to adjust when the question is about a control."
        )
    return (
        "A detector fault may be open. "
        "Name where to adjust. "
        "Write the whole reply in Chinese."
    )


def _facts(snapshot: Snapshot, runbook: Runbook, faults: list[Fault]) -> str:
    mix = runbook.house_mix
    parts = [
        (
            f"House mix in OBS: {mix.obs_name}, Windows device {mix.windows_device}. "
            f"This is one combined X-USB feed, target {mix.target_db:.0f} dB."
        ),
        f"Mixer: {runbook.board_name}. Monitored separately over OSC, not as extra OBS inputs.",
    ]
    report = describe_path(snapshot, runbook)
    parts.append(report.conclusion)
    if snapshot.board is not None and snapshot.board.connected:
        parts.append(report.summary())
    scribble = _scribble_fact(runbook)
    if scribble:
        parts.append(scribble + " These are labels, not a list of what is in the mix right now.")
    reaching = house_channels(snapshot.board, runbook)
    if snapshot.board is not None and snapshot.board.connected:
        if reaching:
            names = ", ".join(label for _strip, label in reaching)
            parts.append(f"Reaching the house mix now: {names}.")
        else:
            parts.append("No channel is reaching the house mix.")
    else:
        parts.append("The X32 is not connected, so active channels are unknown.")
    unmapped = [item.role for item in runbook.channels if item.kind == "channel" and not item.strip]
    if unmapped:
        parts.append("Still unmapped: " + ", ".join(unmapped) + ".")
    if runbook.stream_mix is not None and not runbook.stream_mix.strip:
        parts.append("No dedicated stream bus is assigned. YouTube uses the house mix.")
    elif runbook.stream_mix is not None and runbook.stream_mix.strip.isdigit():
        parts.append(f"Stream mix is {runbook.stream_mix.kind} {int(runbook.stream_mix.strip)}.")
    if runbook.rack:
        parts.append("Not monitored: " + ", ".join(item.name for item in runbook.rack) + ".")
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
    """One model call. No tool round trip and no retry while Flash is busy."""
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
            max_output_tokens=800,
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        ),
    )
    return (response.text or "").strip()

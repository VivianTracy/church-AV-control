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
Name the OBS control, such as the Audio Mixer speaker for Audio Input Capture,
or the Behringer channel when the runbook names it.
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
        model="gemini-flash-latest",
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
    local = local_reply(snapshot, runbook, faults)
    if not api_key.strip():
        return Guide(local, "detector")
    tools = build_tools(snapshot, runbook, faults)
    try:
        if completer is not None:
            reply = completer(_prompt(question), tools)
        else:
            reply = _ask_adk(tools, _prompt(question))
    except Exception:
        return Guide(local, "detector")
    reply = (reply or "").strip()
    if not reply:
        return Guide(local, "detector")
    return Guide(reply, "adk")


def local_reply(snapshot: Snapshot, runbook: Runbook, faults: list[Fault]) -> str:
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


def _prompt(question: str | None) -> str:
    if question:
        return (
            "The operator asked: "
            + question.strip()
            + " Use the tools. Reply in two or three sentences and name where to adjust."
        )
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
        return asyncio.run(asyncio.wait_for(_run(), timeout=8))
    except TimeoutError as exc:
        raise RuntimeError("The guide took too long") from exc

from __future__ import annotations

from church_av.models import BoardReading, Fault, Snapshot
from church_av.path import describe_path
from church_av.runbook import Runbook

_BOARD_DIAGRAM = {
    "house_mix_clipping",
    "house_mix_silent",
    "path_mic_dark",
    "path_stream_dark",
    "path_usb_dark",
}

CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
SURFACE_ID = "booth"


def build_messages(
    snapshot: Snapshot,
    runbook: Runbook,
    faults: list[Fault],
    allow_apply: bool,
    guide: str = "",
    asked: str = "",
) -> list[dict]:
    components: list[dict] = []
    root_children = ["title", "status"]
    components.append(_text("title", runbook.church))
    components.extend(_status(snapshot, runbook))

    if faults:
        root_children.append("fault_heading")
        components.append(_text("fault_heading", "Adjust here"))
        for index, fault in enumerate(faults):
            root_children.append(f"fault_{index}")
            components.extend(_fault_card(index, fault, allow_apply, runbook, snapshot.board))
    elif snapshot.connected:
        root_children.append("clear")
        components.extend(_clear_card())

    root_children.extend(["guide", "chat", "scene_heading", "scene_strip"])
    components.extend(_guide_card(guide, asked, faults))
    components.extend(_chat())
    components.append(_text("scene_heading", "Scenes"))
    components.extend(_scene_strip(snapshot, runbook))

    components.insert(0, _column("root", root_children))
    return [
        {
            "version": "1.0",
            "createSurface": {
                "surfaceId": SURFACE_ID,
                "catalogId": CATALOG_ID,
                "components": components,
                "dataModel": _meter_data(snapshot, runbook),
            },
        }
    ]


def _status(snapshot: Snapshot, runbook: Runbook) -> list[dict]:
    scene = snapshot.program_scene or "No scene"
    note = runbook.scene_note(snapshot.program_scene)
    if not snapshot.connected:
        picture = "Waiting for the service PC"
    elif note:
        picture = note.picture
    else:
        picture = "Scene not in the runbook yet"
    stream_value, stream_detail, stream_icon = _stream_copy(snapshot, runbook)
    mix_value, mix_detail, mix_icon = _mix_copy(snapshot, runbook)
    return [
        _row("status", ["scene_card", "stream_card", "mix_card"]),
        _card("scene_card", "scene_body"),
        _column("scene_body", ["scene_icon", "scene_kicker", "scene_value", "scene_detail"]),
        _icon("scene_icon", "camera"),
        _text("scene_kicker", "On screen", "caption"),
        _text("scene_value", scene),
        _text("scene_detail", picture, "caption"),
        _card("stream_card", "stream_body"),
        _column("stream_body", ["stream_icon", "stream_kicker", "stream_value", "stream_detail"]),
        _icon("stream_icon", stream_icon),
        _text("stream_kicker", "YouTube", "caption"),
        _text("stream_value", stream_value),
        _text("stream_detail", stream_detail, "caption"),
        _card("mix_card", "mix_body"),
        _column("mix_body", ["mix_icon", "mix_kicker", "mix_value", "mix_detail", "meter"]),
        _icon("mix_icon", mix_icon),
        _text("mix_kicker", "House mix", "caption"),
        _text("mix_value", mix_value),
        _text("mix_detail", mix_detail, "caption"),
        _row("meter", ["meter_caption"]),
        _text("meter_caption", "X-USB meter", "caption"),
    ]


def _scene_strip(snapshot: Snapshot, runbook: Runbook) -> list[dict]:
    known = [scene.name for scene in runbook.scenes]
    names = known or snapshot.scenes
    if snapshot.program_scene and snapshot.program_scene not in names:
        names = [*names, snapshot.program_scene]

    def sort_key(name: str) -> tuple:
        head = name.split("-", 1)[0]
        return (int(head), name) if head.isdigit() else (999, name)

    ordered = sorted(names, key=sort_key)
    ids = []
    components: list[dict] = []
    for index, name in enumerate(ordered):
        card_id = "scene_current" if name == snapshot.program_scene else f"scene_{index}"
        ids.append(card_id)
        components.append(_card(card_id, f"{card_id}_label"))
        components.append(_text(f"{card_id}_label", name, "caption"))
    components.insert(0, _row("scene_strip", ids))
    return components


def _guide_card(reply: str, asked: str, faults: list[Fault]) -> list[dict]:
    show_speaker = bool(asked) and any(fault.id == "house_mix_muted" for fault in faults)
    children: list[str] = []
    components: list[dict] = []
    if show_speaker:
        children.append("guide_icon")
        components.append(_icon("guide_icon", "volumeMute"))
    children.append("guide_kicker")
    components.append(_text("guide_kicker", "Guide", "caption"))
    if asked:
        children.append("guide_asked")
        components.append(_text("guide_asked", f"You asked: {asked}", "caption"))
    children.append("guide_reply")
    if asked:
        shown = reply or "Ask about what you see."
    elif faults and reply:
        shown = reply
    else:
        shown = "Ask about what you see."
    components.append(_text("guide_reply", shown))
    return [_card("guide", "guide_body"), _column("guide_body", children), *components]


def _chat() -> list[dict]:
    return [
        _row("chat", ["question", "ask"]),
        {
            "id": "question",
            "component": "TextField",
            "label": "Ask the booth",
            "text": "",
            "textFieldType": "shortText",
        },
        _text("ask_label", "Ask"),
        {
            "id": "ask",
            "component": "Button",
            "child": "ask_label",
            "variant": "primary",
            "action": {"event": {"name": "ask", "context": {}}},
        },
    ]


def _clear_card() -> list[dict]:
    return [
        _card("clear", "clear_body"),
        _row("clear_body", ["clear_icon", "clear_text"]),
        _icon("clear_icon", "check"),
        _text("clear_text", "Readings look normal."),
    ]


def _fault_card(
    index: int,
    fault: Fault,
    allow_apply: bool,
    runbook: Runbook,
    board: BoardReading | None = None,
) -> list[dict]:
    if fault.id in _BOARD_DIAGRAM:
        return _board_fault_card(index, fault, runbook, board)
    place, control, icon_name = _guide_place(fault)
    graphic, mark = _graphic(fault)
    copy_ids = [f"fault_{index}_place", f"fault_{index}_control", f"fault_{index}_body"]
    components: list[dict] = [
        _card(f"fault_{index}", f"fault_{index}_row"),
        _row(f"fault_{index}_row", [f"fault_{index}_{graphic}", f"fault_{index}_copy"]),
        _column(f"fault_{index}_{graphic}", [f"fault_{index}_icon", f"fault_{index}_where"]),
        _icon(f"fault_{index}_icon", icon_name),
        _text(f"fault_{index}_where", mark, "caption"),
        _column(f"fault_{index}_copy", copy_ids),
        _text(f"fault_{index}_place", place, "caption"),
        _text(f"fault_{index}_control", control),
        _text(f"fault_{index}_body", fault.evidence),
    ]
    for step_index, step in enumerate(fault.steps):
        step_id = f"fault_{index}_step_{step_index}"
        copy_ids.append(step_id)
        components.append(_text(step_id, step))
    if allow_apply and fault.action is not None:
        label_id = f"fault_{index}_apply_label"
        button_id = f"fault_{index}_apply"
        copy_ids.append(button_id)
        components.append(_text(label_id, fault.action.summary))
        components.append(
            {
                "id": button_id,
                "component": "Button",
                "child": label_id,
                "variant": "primary",
                "action": {
                    "event": {
                        "name": "apply_fix",
                        "context": {"faultId": fault.id},
                    }
                },
            }
        )
    return components


def _board_fault_card(
    index: int,
    fault: Fault,
    runbook: Runbook,
    board: BoardReading | None = None,
) -> list[dict]:
    place, control, _icon_name = _guide_place(fault)
    direction = "down" if fault.id == "house_mix_clipping" else "up"
    highlight = _highlight(fault, runbook)
    copy_ids = [f"fault_{index}_place", f"fault_{index}_control", f"fault_{index}_body"]
    components: list[dict] = [
        _card(f"fault_{index}", f"fault_{index}_stack"),
        _column(f"fault_{index}_stack", [f"fault_{index}_board", f"fault_{index}_copy"]),
        _x32_diagram(f"fault_{index}_board", runbook, highlight, direction, board),
        _column(f"fault_{index}_copy", copy_ids),
        _text(f"fault_{index}_place", place, "caption"),
        _text(f"fault_{index}_control", control),
        _text(f"fault_{index}_body", fault.evidence),
    ]
    for step_index, step in enumerate(fault.steps):
        step_id = f"fault_{index}_step_{step_index}"
        copy_ids.append(step_id)
        components.append(_text(step_id, step))
    return components


def _highlight(fault: Fault, runbook: Runbook) -> str:
    if fault.id == "path_stream_dark":
        return "stream"
    if fault.id == "path_mic_dark":
        for item in runbook.channels:
            if item.kind == "channel" and item.strip:
                return item.id
        return "pulpit"
    return "main"


def _x32_diagram(
    component_id: str,
    runbook: Runbook,
    highlight: str,
    direction: str,
    board: BoardReading | None = None,
) -> dict:
    channels = [item for item in runbook.channels if item is not None]
    if highlight == "stream" and runbook.stream_mix is not None:
        channels.append(runbook.stream_mix)
    if not channels:
        found = runbook.channel(highlight)
        channels = [] if found is None else [found]
    strips = [
        {
            "id": item.id,
            "role": item.role,
            "name": runbook.resolved_name(item.id, board) or "",
        }
        for item in channels
    ]
    return {
        "id": component_id,
        "component": "X32Diagram",
        "board": runbook.board_name,
        "highlight": highlight,
        "direction": direction,
        "channels": strips,
    }


def _graphic(fault: Fault) -> tuple[str, str]:
    if fault.id in _BOARD_DIAGRAM:
        return "badge", "Board"
    if fault.id == "path_obs_dark" or fault.id.startswith("house_mix"):
        return "fader", "X-USB"
    if fault.id == "obs_disconnected":
        return "badge", "PC"
    return "badge", "OBS"


def _guide_place(fault: Fault) -> tuple[str, str, str]:
    if fault.id == "path_obs_dark":
        return "OBS Audio Mixer", "Audio Input Capture", "volumeUp"
    if fault.id in _BOARD_DIAGRAM:
        question = fault.steps[0] if fault.steps else "Which knob?"
        return "Behringer X32", question, "volumeUp"
    if fault.id in {"house_mix_muted", "house_mix_fader_low"}:
        icon_name = "volumeMute" if fault.id == "house_mix_muted" else "volumeUp"
        return "OBS Audio Mixer", "Audio Input Capture", icon_name
    if fault.id in {"stream_stopped", "stream_reconnecting", "frames_skipped"}:
        return "OBS controls", "Stream button", "play"
    if fault.id == "obs_disconnected":
        return "Service PC", "OBS WebSocket", "warning"
    return "OBS", fault.title, "warning"


def _stream_copy(snapshot: Snapshot, runbook: Runbook) -> tuple[str, str, str]:
    if not snapshot.connected:
        return "Offline", "Cannot read the service PC", "warning"
    if snapshot.streaming:
        return "Live", "OBS is sending", "play"
    if snapshot.reconnecting:
        return "Reconnecting", "The upload dropped", "warning"
    if runbook.expect_stream_live:
        return "Stopped", "The service should be live", "stop"
    return "Stopped", "Expected while you are not live", "stop"


def _mix_copy(snapshot: Snapshot, runbook: Runbook) -> tuple[str, str, str]:
    house = snapshot.input_named(runbook.house_mix.obs_name)
    raw = runbook.house_mix.windows_device or runbook.house_mix.obs_name
    device = raw.rsplit("(", 1)[-1].rstrip(")") if "(" in raw else raw
    if house is None:
        return "Missing", device, "warning"
    if house.muted:
        detail = f"Muted · {device}"
        return "Muted", detail, "volumeMute"
    if house.volume_db is None:
        value, detail = "Open", device
    else:
        value, detail = "Open", f"Fader {house.volume_db:.0f} dB · {device}"
    if snapshot.board is not None and snapshot.board.connected:
        detail = f"{detail} · {describe_path(snapshot, runbook).summary()}"
    return value, detail, "volumeUp"


def _meter_data(snapshot: Snapshot, runbook: Runbook) -> dict:
    house = snapshot.input_named(runbook.house_mix.obs_name)
    return {
        "meterDb": None if house is None else house.meter_db,
        "faderDb": None if house is None else house.volume_db,
        "muted": bool(house and house.muted),
        "connected": snapshot.connected,
    }


def _text(component_id: str, text: str, variant: str = "body") -> dict:
    return {"id": component_id, "component": "Text", "text": text, "variant": variant}


def _icon(component_id: str, name: str) -> dict:
    return {"id": component_id, "component": "Icon", "name": name}


def _row(component_id: str, children: list[str]) -> dict:
    return {"id": component_id, "component": "Row", "children": children, "align": "stretch"}


def _column(component_id: str, children: list[str]) -> dict:
    return {"id": component_id, "component": "Column", "children": children}


def _card(component_id: str, child: str) -> dict:
    return {"id": component_id, "component": "Card", "child": child}

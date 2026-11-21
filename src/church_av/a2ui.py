from __future__ import annotations

from church_av.models import Fault, Snapshot
from church_av.runbook import Runbook

CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
SURFACE_ID = "booth"


def build_messages(
    snapshot: Snapshot,
    runbook: Runbook,
    faults: list[Fault],
    allow_apply: bool,
) -> list[dict]:
    components: list[dict] = []
    root_children: list[str] = ["title", "scene_line", "stream_line", "mix_line"]

    components.append(_text("title", runbook.church))
    scene = snapshot.program_scene or "No scene"
    note = runbook.scene_note(snapshot.program_scene)
    picture = f" — {note.picture}" if note else ""
    components.append(_text("scene_line", f"On screen: {scene}{picture}"))
    if not snapshot.connected:
        stream_text = "Service machine: not connected"
    elif snapshot.streaming:
        stream_text = "YouTube output: live"
    elif snapshot.reconnecting:
        stream_text = "YouTube output: reconnecting"
    elif runbook.expect_stream_live:
        stream_text = "YouTube output: stopped"
    else:
        stream_text = "YouTube output: stopped, and that is expected right now"
    components.append(_text("stream_line", stream_text))
    components.append(_text("mix_line", _mix_line(snapshot, runbook)))

    if faults:
        root_children.append("fault_heading")
        components.append(_text("fault_heading", "Needs attention"))
        for index, fault in enumerate(faults):
            card_id = f"fault_{index}"
            root_children.append(card_id)
            components.extend(_fault_card(index, fault, allow_apply))
    else:
        root_children.append("clear")
        components.append(_text("clear", "Readings look normal."))

    root_children.append("layout_heading")
    components.append(_text("layout_heading", "Layouts this computer knows", variant="caption"))
    layout_ids = _layout_lines(components, snapshot, runbook)
    root_children.extend(layout_ids)

    components.insert(0, {"id": "root", "component": "Column", "children": root_children})
    return [
        {
            "version": "1.0",
            "createSurface": {
                "surfaceId": SURFACE_ID,
                "catalogId": CATALOG_ID,
                "components": components,
            },
        }
    ]


def _mix_line(snapshot: Snapshot, runbook: Runbook) -> str:
    mix = runbook.house_mix
    house = snapshot.input_named(mix.obs_name)
    if house is None:
        return f"House mix: {mix.obs_name} is missing"
    if house.muted:
        state = "muted"
    else:
        state = "open"
    fader = f", fader {house.volume_db:.0f} dB" if house.volume_db is not None else ""
    meter = f", meter {house.meter_db:.0f} dB" if house.meter_db is not None else ""
    return f"House mix ({mix.windows_device}): {state}{fader}{meter}"


def _fault_card(index: int, fault: Fault, allow_apply: bool) -> list[dict]:
    body_id = f"fault_{index}_body"
    steps_id = f"fault_{index}_steps"
    column_id = f"fault_{index}_column"
    children = [f"fault_{index}_title", body_id, steps_id]
    components = [
        {"id": f"fault_{index}", "component": "Card", "child": column_id},
        {"id": column_id, "component": "Column", "children": children},
        _text(f"fault_{index}_title", fault.title),
        _text(body_id, fault.evidence),
        _text(steps_id, " ".join(fault.steps)),
    ]
    if allow_apply and fault.action is not None:
        label_id = f"fault_{index}_apply_label"
        button_id = f"fault_{index}_apply"
        children.append(button_id)
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


def _layout_lines(components: list[dict], snapshot: Snapshot, runbook: Runbook) -> list[str]:
    known = {scene.name: scene.picture for scene in runbook.scenes}
    names = snapshot.scenes or list(known)
    ids: list[str] = []

    def sort_key(name: str) -> tuple:
        head = name.split("-", 1)[0]
        return (int(head), name) if head.isdigit() else (999, name)

    for index, name in enumerate(sorted(names, key=sort_key)):
        picture = known.get(name)
        if picture:
            line = f"{name}: {picture}"
        else:
            line = f"{name}: OBS has this scene, and the runbook does not describe it yet."
        component_id = f"layout_{index}"
        components.append(_text(component_id, line, variant="caption"))
        ids.append(component_id)
    return ids


def _text(component_id: str, text: str, variant: str = "body") -> dict:
    return {"id": component_id, "component": "Text", "text": text, "variant": variant}

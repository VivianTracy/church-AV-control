"""The only place an OBS change is allowed. The agent may name an action; this module keeps it or drops it."""

from __future__ import annotations

from dataclasses import dataclass

from church_av.models import Action, Snapshot
from church_av.runbook import Runbook

MAX_FADER_STEP_DB = 3.0
MAX_FADER_DB = 0.0

_STREAM_KINDS = {
    "start_stream",
    "stop_stream",
    "toggle_stream",
    "go_live",
}
_BLOCKED_KINDS = {
    "set_stream_settings",
    "remove_input",
    "delete_source",
    "board",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    action: Action | None = None


def decide(
    proposal: Action | None,
    snapshot: Snapshot,
    runbook: Runbook,
    *,
    confirmed: bool = False,
) -> Decision:
    if proposal is None:
        return Decision(False, "There is no change to apply.")
    if proposal.kind in _STREAM_KINDS:
        return Decision(False, "Starting or stopping the stream stays on the service PC.")
    if proposal.kind in _BLOCKED_KINDS:
        return Decision(False, "That change is not available from the booth.")
    if not snapshot.connected:
        return Decision(False, "OBS is not connected, so nothing can be changed.")

    if proposal.kind == "set_input_mute":
        return _mute(proposal, runbook)
    if proposal.kind == "set_input_volume":
        return _volume(proposal, snapshot, runbook)
    if proposal.kind == "set_program_scene":
        return _scene(proposal, snapshot, confirmed)
    return Decision(False, "That change is not available from the booth.")


def _mute(proposal: Action, runbook: Runbook) -> Decision:
    mix_name = runbook.house_mix.obs_name
    if proposal.input_name != mix_name:
        return Decision(
            False,
            f"Only {mix_name} can be changed. Camera inputs stay muted.",
        )
    if proposal.muted is not False:
        return Decision(False, "Muting the house mix from here is disabled.")
    return Decision(True, proposal.summary, proposal)


def _volume(proposal: Action, snapshot: Snapshot, runbook: Runbook) -> Decision:
    mix_name = runbook.house_mix.obs_name
    if proposal.input_name != mix_name:
        return Decision(False, f"Only the {mix_name} fader can be moved.")
    if proposal.volume_db is None:
        return Decision(False, "A fader move needs a level in dB.")
    if proposal.volume_db > MAX_FADER_DB:
        return Decision(False, "The house-mix fader cannot go above 0 dB.")

    house = snapshot.input_named(mix_name)
    if house is None or house.volume_db is None:
        return Decision(False, "The house-mix fader level is not in this reading.")

    delta = proposal.volume_db - house.volume_db
    if abs(delta) <= MAX_FADER_STEP_DB:
        target = proposal.volume_db
    else:
        target = house.volume_db + (MAX_FADER_STEP_DB if delta > 0 else -MAX_FADER_STEP_DB)
    if target > MAX_FADER_DB:
        return Decision(False, "The house-mix fader cannot go above 0 dB.")
    if abs(target - house.volume_db) < 0.05:
        return Decision(False, "The house-mix fader is already at that level.")

    action = Action(
        kind="set_input_volume",
        summary=f"Set {mix_name} to {target:.1f} dB",
        input_name=mix_name,
        volume_db=target,
    )
    return Decision(True, action.summary, action)


def _scene(proposal: Action, snapshot: Snapshot, confirmed: bool) -> Decision:
    if not confirmed:
        return Decision(False, "A scene change waits until the operator confirms it in chat.")
    name = proposal.scene_name
    if not name or name not in snapshot.scenes:
        return Decision(False, "That scene is not on the service PC.")
    if name == snapshot.program_scene:
        return Decision(False, f"{name} is already the program scene.")
    action = Action(
        kind="set_program_scene",
        summary=f"Switch the program scene to {name}",
        scene_name=name,
    )
    return Decision(True, action.summary, action)

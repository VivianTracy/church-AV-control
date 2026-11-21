"""Compare the mic, the stream bus, the X32 USB send, and the OBS input."""

from __future__ import annotations

from dataclasses import dataclass

from church_av.models import BoardReading, Snapshot, StripReading
from church_av.runbook import Runbook

QUIET_DB = -55.0


@dataclass(frozen=True)
class Stage:
    id: str
    state: str
    detail: str
    label: str
    reading: str
    highlight: str = ""


@dataclass(frozen=True)
class PathReport:
    stages: tuple[Stage, ...]
    conclusion: str
    fault_id: str
    highlight: str

    def summary(self) -> str:
        return " · ".join(f"{stage.label} {stage.reading}" for stage in self.stages)


def describe_path(snapshot: Snapshot, runbook: Runbook) -> PathReport:
    """Say where the audio stopped. A quiet mic is not a fault while OBS still has level."""
    obs = _obs_stage(snapshot, runbook)
    board = snapshot.board
    if board is None or not board.connected:
        stages = (
            Stage("mic", "unread", "The mic meter was not read over OSC.", "Mic", "no meter"),
            Stage("stream", "unread", "The stream bus was not read over OSC.", "Stream", "no meter"),
            Stage("usb", "unread", "The X32 USB send was not read over OSC.", "USB", "no meter"),
            obs,
        )
        return PathReport(
            stages=stages,
            conclusion="The X32 meters were not read over OSC. OBS shows one combined X-USB feed.",
            fault_id="house_mix_silent",
            highlight="main",
        )
    mic = _mic_stage(board, runbook)
    stream = _stream_stage(board, runbook)
    usb = _usb_stage(board, runbook)
    stages = (mic, stream, usb, obs)
    fault_id, conclusion, highlight = _conclude(mic, stream, usb, obs)
    return PathReport(stages=stages, conclusion=conclusion, fault_id=fault_id, highlight=highlight)


def _conclude(mic: Stage, stream: Stage, usb: Stage, obs: Stage) -> tuple[str, str, str]:
    if obs.state == "hot":
        return "", "The X-USB feed is reaching OBS.", "main"
    if mic.state == "quiet":
        return "path_mic_dark", "The mic isn't reaching the mixer.", mic.highlight or "pulpit"
    if stream.state == "quiet":
        return "path_stream_dark", "The mic is reaching the mixer, and the stream bus is quiet.", "stream"
    if usb.state == "quiet":
        return "path_usb_dark", "The mic is up, and the X32 USB send is quiet.", "main"
    if usb.state == "hot" and obs.state == "quiet":
        return "path_obs_dark", "The mixer audio isn't reaching OBS.", "main"
    if mic.state == "unmapped":
        return (
            "house_mix_silent",
            "No mic channel is mapped, so the mic stage cannot be checked.",
            "main",
        )
    return (
        "house_mix_silent",
        "The OBS X-USB input is quiet, and an X32 meter was missing from the reading.",
        "main",
    )


def _obs_stage(snapshot: Snapshot, runbook: Runbook) -> Stage:
    mix = runbook.house_mix
    house = snapshot.input_named(mix.obs_name)
    label = "OBS"
    if house is None:
        return Stage("obs", "unread", f"OBS has no input named {mix.obs_name}.", label, "missing")
    if house.muted:
        return Stage("obs", "quiet", f"{mix.obs_name} is muted in OBS.", label, "muted")
    if house.meter_db is None:
        return Stage("obs", "unread", f"{mix.obs_name} has no meter in this reading.", label, "no meter")
    reading = _db(house.meter_db)
    device = mix.windows_device or mix.obs_name
    if house.meter_db <= QUIET_DB:
        return Stage(
            "obs",
            "quiet",
            f"OBS {mix.obs_name} ({device}) is at the noise floor.",
            label,
            reading,
        )
    return Stage("obs", "hot", f"OBS {mix.obs_name} ({device}) is at {reading}.", label, reading)


def _mic_stage(board: BoardReading, runbook: Runbook) -> Stage:
    mics = [item for item in runbook.channels if item.kind == "channel" and "mic" in item.role.lower()]
    mapped = [item for item in mics if runbook.resolved_strip(item.id) is not None]
    unmapped = [item.role for item in mics if runbook.resolved_strip(item.id) is None]
    if not mapped:
        note = f" Unmapped: {', '.join(unmapped)}." if unmapped else ""
        return Stage("mic", "unmapped", f"No mic strip is mapped.{note}", "Mic", "not mapped")
    quiet: list[str] = []
    hot: list[str] = []
    quiet_highlight = mapped[0].id
    hot_highlight = mapped[0].id
    quiet_reading = ""
    hot_reading = ""
    for item in mapped:
        number = runbook.resolved_strip(item.id)
        strip = _find(board, item.kind, number)
        name = runbook.resolved_name(item.id, board) or item.role
        if strip is None or (strip.meter_db is None and strip.on is None):
            continue
        if strip.on is False or (strip.meter_db is not None and strip.meter_db <= QUIET_DB):
            if strip.on is False:
                quiet.append(f"{name}, channel {number}, is muted")
                quiet_reading = "muted"
            else:
                quiet.append(f"{name}, channel {number}, is at the noise floor")
                quiet_reading = _db(strip.meter_db)
            quiet_highlight = item.id
        elif strip.meter_db is not None:
            hot.append(f"{name}, channel {number}, is at {_db(strip.meter_db)}")
            hot_highlight = item.id
            hot_reading = _db(strip.meter_db)
    if len(mapped) == 1:
        label = runbook.resolved_name(mapped[0].id, board) or "Mic"
    else:
        label = "Mics"
    extra = f" Not mapped: {', '.join(unmapped)}." if unmapped else ""
    if hot:
        return Stage("mic", "hot", f"{'; '.join(hot)}.{extra}", label, hot_reading, hot_highlight)
    if quiet:
        return Stage("mic", "quiet", f"{'; '.join(quiet)}.{extra}", label, quiet_reading or "no meter", quiet_highlight)
    return Stage("mic", "unread", "The mapped mic meter was not in the X32 reading.", label, "no meter", mapped[0].id)


def _stream_stage(board: BoardReading, runbook: Runbook) -> Stage:
    item = runbook.stream_mix
    if item is None or runbook.resolved_strip(item.id) is None:
        return Stage(
            "stream",
            "skipped",
            "No stream bus is assigned. YouTube still uses the house mix on X-USB.",
            "Stream",
            "house mix",
        )
    number = runbook.resolved_strip(item.id)
    strip = _find(board, item.kind, number)
    name = runbook.resolved_name(item.id, board) or f"{item.kind} {number}"
    if strip is None or strip.meter_db is None:
        return Stage("stream", "unread", f"{name} had no meter in the X32 reading.", "Stream", "no meter")
    reading = _db(strip.meter_db)
    if strip.meter_db <= QUIET_DB:
        return Stage("stream", "quiet", f"{name} is at the noise floor.", "Stream", reading)
    return Stage("stream", "hot", f"{name} is at {reading}.", "Stream", reading)


def _usb_stage(board: BoardReading, runbook: Runbook) -> Stage:
    source = runbook.usb_source or "main"
    if source == "main":
        meters = [item.meter_db for item in board.main if item.index in {1, 2} and item.meter_db is not None]
        if not meters:
            return Stage("usb", "unread", "The main L/R meter was not in the X32 reading.", "USB", "no meter")
        level = max(meters)
        reading = _db(level)
        if level <= QUIET_DB:
            return Stage("usb", "quiet", "Main L/R, the USB send, is at the noise floor.", "USB", reading)
        return Stage("usb", "hot", f"Main L/R, the USB send, is at {reading}.", "USB", reading)
    item = runbook.stream_mix
    number = None if item is None else runbook.resolved_strip(item.id)
    if item is None or number is None or item.kind != source:
        return Stage(
            "usb",
            "unmapped",
            f"The USB card is set to a {source}, and that strip is not mapped.",
            "USB",
            "not mapped",
        )
    strip = _find(board, source, number)
    if strip is None or strip.meter_db is None:
        return Stage("usb", "unread", "The USB send meter was not in the X32 reading.", "USB", "no meter")
    reading = _db(strip.meter_db)
    if strip.meter_db <= QUIET_DB:
        return Stage("usb", "quiet", f"The USB send ({source} {number}) is at the noise floor.", "USB", reading)
    return Stage("usb", "hot", f"The USB send ({source} {number}) is at {reading}.", "USB", reading)


def _find(board: BoardReading, kind: str, number: int | None) -> StripReading | None:
    if number is None:
        return None
    if kind == "channel":
        pool = board.channels
    elif kind == "bus":
        pool = board.buses
    elif kind == "matrix":
        pool = board.matrices
    elif kind == "main":
        pool = board.main
    else:
        return None
    for item in pool:
        if item.index == number:
            return item
    return None


def _db(meter: float | None) -> str:
    if meter is None:
        return "no meter"
    return f"{meter:.0f} dB"

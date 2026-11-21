from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InputReading:
    name: str
    kind: str = ""
    muted: bool | None = None
    volume_db: float | None = None
    meter_db: float | None = None


@dataclass
class StripReading:
    """One X32 channel, bus, matrix, or main output. OSC only. Not an OBS input."""

    kind: str
    index: int
    name: str = ""
    meter_db: float | None = None
    on: bool | None = None


@dataclass
class BoardReading:
    connected: bool
    error: str | None = None
    channels: list[StripReading] = field(default_factory=list)
    buses: list[StripReading] = field(default_factory=list)
    matrices: list[StripReading] = field(default_factory=list)
    main: list[StripReading] = field(default_factory=list)


@dataclass
class Snapshot:
    connected: bool
    error: str | None = None
    obs_version: str | None = None
    platform: str | None = None
    program_scene: str | None = None
    scenes: list[str] = field(default_factory=list)
    streaming: bool = False
    reconnecting: bool = False
    output_skipped_frames: int = 0
    output_total_frames: int = 0
    cpu_percent: float | None = None
    inputs: list[InputReading] = field(default_factory=list)
    board: BoardReading | None = None

    def input_named(self, name: str) -> InputReading | None:
        for item in self.inputs:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class Action:
    kind: str
    summary: str
    input_name: str | None = None
    muted: bool | None = None
    volume_db: float | None = None
    scene_name: str | None = None


@dataclass
class Fault:
    id: str
    severity: str
    title: str
    evidence: str
    steps: list[str]
    action: Action | None = None

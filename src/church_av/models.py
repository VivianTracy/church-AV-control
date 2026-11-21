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


@dataclass
class Fault:
    id: str
    severity: str
    title: str
    evidence: str
    steps: list[str]
    action: Action | None = None

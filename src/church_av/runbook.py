from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HouseMix:
    obs_name: str
    windows_device: str
    role: str
    target_db: float
    warn_below_db: float


@dataclass(frozen=True)
class SourceNote:
    obs_name: str
    role: str
    audio: str


@dataclass(frozen=True)
class SceneNote:
    name: str
    picture: str


@dataclass(frozen=True)
class BoardChannel:
    id: str
    role: str
    name: str = ""

    @property
    def known(self) -> bool:
        return bool(self.name.strip())


@dataclass(frozen=True)
class Runbook:
    church: str
    service_name: str
    expect_stream_live: bool
    house_mix: HouseMix
    sources: list[SourceNote] = field(default_factory=list)
    scenes: list[SceneNote] = field(default_factory=list)
    board_name: str = "Behringer X32"
    channels: list[BoardChannel] = field(default_factory=list)

    def scene_note(self, name: str | None) -> SceneNote | None:
        if not name:
            return None
        for scene in self.scenes:
            if scene.name == name:
                return scene
        return None

    def source_note(self, name: str) -> SourceNote | None:
        for source in self.sources:
            if source.obs_name == name:
                return source
        return None

    def channel(self, channel_id: str) -> BoardChannel | None:
        for item in self.channels:
            if item.id == channel_id:
                return item
        return None

    def board_instruction(self, channel_id: str, named: str) -> str:
        """Use the knob name from the runbook. An empty name asks instead of guessing."""
        item = self.channel(channel_id)
        if item is not None and item.known:
            return named.format(name=item.name.strip(), board=self.board_name)
        role = item.role if item is not None else channel_id
        return (
            f"What is the {role} called on the {self.board_name}? "
            "The name is not in the runbook yet."
        )


def load_runbook(path: Path) -> Runbook:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mix = data["house_mix"]
    return Runbook(
        church=data.get("church", "Church stream"),
        service_name=data.get("service_name", "Sunday service"),
        expect_stream_live=bool(data.get("expect_stream_live", False)),
        house_mix=HouseMix(
            obs_name=mix["obs_name"],
            windows_device=mix.get("windows_device", ""),
            role=" ".join(str(mix.get("role", "")).split()),
            target_db=float(mix.get("target_db", -2)),
            warn_below_db=float(mix.get("warn_below_db", -20)),
        ),
        sources=[
            SourceNote(
                obs_name=item["obs_name"],
                role=item.get("role", ""),
                audio=item.get("audio", ""),
            )
            for item in data.get("sources", [])
        ],
        scenes=[
            SceneNote(name=item["name"], picture=item.get("picture", ""))
            for item in data.get("scenes", [])
        ],
        board_name=str((data.get("board") or {}).get("name") or "Behringer X32"),
        channels=[
            BoardChannel(
                id=item["id"],
                role=str(item.get("role") or item["id"]),
                name=str(item.get("name") or "").strip(),
            )
            for item in (data.get("board") or {}).get("channels", [])
        ],
    )

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from church_av.models import BoardReading


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
    kind: str = "channel"
    strip: str = ""

    @property
    def known(self) -> bool:
        return bool(self.name.strip())


@dataclass(frozen=True)
class BoardLabel:
    """A scribble read off the console. A factory Ch## label is not stored."""

    strip: int
    name: str
    kind: str = "channel"


@dataclass(frozen=True)
class RackItem:
    name: str
    note: str


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
    board_link: str = "osc"
    usb_source: str = "main"
    stream_mix: BoardChannel | None = None
    labels: list[BoardLabel] = field(default_factory=list)
    rack: list[RackItem] = field(default_factory=list)

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

    def entry(self, channel_id: str) -> BoardChannel | None:
        found = self.channel(channel_id)
        if found is not None:
            return found
        if self.stream_mix is not None and self.stream_mix.id == channel_id:
            return self.stream_mix
        return None

    def resolved_strip(self, channel_id: str) -> int | None:
        item = self.entry(channel_id)
        if item is None or item.kind == "main" or not item.strip.isdigit():
            return None
        return int(item.strip)

    def scribble(self, kind: str, strip: int) -> str:
        for item in self.labels:
            if item.kind == kind and item.strip == strip and item.name.strip():
                return item.name.strip()
        return ""

    def resolved_name(self, channel_id: str, board: BoardReading | None = None) -> str | None:
        """Console name, then the runbook name, then a scribble read off the board."""
        item = self.entry(channel_id)
        if item is None:
            return None
        number = self.resolved_strip(channel_id)
        if board is not None and board.connected and number is not None:
            live = _live_name(board, item.kind, number)
            if live:
                return live
        if item.kind == "main" and board is not None and board.connected:
            for strip in board.main:
                if strip.name.strip():
                    return strip.name.strip()
        if item.known:
            return item.name.strip()
        if number is not None:
            photo = self.scribble(item.kind, number)
            if photo:
                return photo
        return None

    def board_instruction(self, channel_id: str, named: str, board: BoardReading | None = None) -> str:
        """Use a known knob name. An empty name asks instead of guessing a strip."""
        item = self.entry(channel_id)
        label = self.resolved_name(channel_id, board)
        if label:
            return named.format(name=label, board=self.board_name)
        if (
            board is not None
            and board.connected
            and item is not None
            and item.kind != "main"
            and not item.strip
        ):
            return (
                f"Which {item.kind} is the {item.role} on the {self.board_name}? "
                "The console did not name it, so enter the mapping."
            )
        role = item.role if item is not None else channel_id
        return (
            f"What is the {role} called on the {self.board_name}? "
            "The name is not in the runbook yet."
        )

    def with_strip(self, channel_id: str, strip: str) -> Runbook:
        number = str(int(strip))
        if self.stream_mix is not None and self.stream_mix.id == channel_id:
            return replace(self, stream_mix=replace(self.stream_mix, strip=number))
        return replace(
            self,
            channels=[
                replace(item, strip=number) if item.id == channel_id else item for item in self.channels
            ],
        )


def write_strip(path: Path, channel_id: str, strip: str) -> None:
    """Record an operator mapping. This edits the runbook, not the mixer."""
    number = str(int(strip))
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if _is_id_line(line, channel_id)), None)
    if start is None:
        raise ValueError(f"{channel_id} is not in the runbook")
    for index in range(start + 1, min(start + 8, len(lines))):
        stripped = lines[index].strip()
        if stripped.startswith("- id:") or re.fullmatch(r"id: \S+", stripped):
            break
        if not stripped.startswith("strip:"):
            continue
        body = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
        newline = "\n" if lines[index].endswith("\n") else ""
        indent = body[: len(body) - len(body.lstrip(" "))]
        lines[index] = f'{indent}strip: "{number}"{newline}'
        path.write_text("".join(lines), encoding="utf-8")
        return
    raise ValueError(f"{channel_id} has no strip line")


def load_runbook(path: Path) -> Runbook:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mix = data["house_mix"]
    board = data.get("board") or {}
    stream = board.get("stream_mix")
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
        board_name=str(board.get("name") or "Behringer X32"),
        channels=[_channel(item, "channel") for item in board.get("channels", [])],
        board_link=str(board.get("link") or "osc"),
        usb_source=str(board.get("usb_source") or "main"),
        stream_mix=None if not stream else _channel(stream, "bus"),
        labels=[
            BoardLabel(
                strip=int(item["strip"]),
                name=str(item.get("name") or "").strip(),
                kind=str(item.get("kind") or "channel"),
            )
            for item in board.get("labels", [])
        ],
        rack=[
            RackItem(name=str(item["name"]), note=" ".join(str(item.get("note") or "").split()))
            for item in data.get("rack", [])
        ],
    )


def _channel(item: dict, default_kind: str) -> BoardChannel:
    return BoardChannel(
        id=str(item["id"]),
        role=str(item.get("role") or item["id"]),
        name=str(item.get("name") or "").strip(),
        kind=str(item.get("kind") or default_kind),
        strip=str(item.get("strip") or "").strip(),
    )


def _is_id_line(line: str, channel_id: str) -> bool:
    return re.fullmatch(rf"(?:- )?id: {re.escape(channel_id)}", line.strip()) is not None


def _live_name(board: BoardReading, kind: str, number: int) -> str:
    if kind == "channel":
        pool = board.channels
    elif kind == "bus":
        pool = board.buses
    elif kind == "matrix":
        pool = board.matrices
    else:
        return ""
    for item in pool:
        if item.index == number and item.name.strip():
            return item.name.strip()
    return ""

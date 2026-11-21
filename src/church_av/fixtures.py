"""Load OBS snapshots saved from the 20 Nov 2026 rehearsal."""

from __future__ import annotations

import json
from pathlib import Path

from church_av.models import InputReading, Snapshot

FIXTURE_NAMES = (
    "healthy",
    "muted_house_mix",
    "low_fader",
    "live_stream",
    "dropped_frames",
    "clipping",
)


def fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures"


def load_fixture(name: str) -> list[Snapshot]:
    path = fixtures_dir() / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_snapshots = data.get("snapshots") or [data["snapshot"]]
    return [snapshot_from_dict(item) for item in raw_snapshots]


def load_fixture_record(name: str) -> dict:
    path = fixtures_dir() / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_from_dict(raw: dict) -> Snapshot:
    inputs = [InputReading(**item) for item in raw.get("inputs", [])]
    fields = {key: value for key, value in raw.items() if key != "inputs"}
    return Snapshot(inputs=inputs, **fields)

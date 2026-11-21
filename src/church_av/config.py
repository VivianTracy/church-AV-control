from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path) -> None:
    """Load KEY=VALUE pairs. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    obs_host: str
    obs_port: int
    obs_password: str
    allow_apply: bool
    booth_host: str
    booth_port: int
    poll_seconds: float
    runbook_path: Path
    root: Path
    google_api_key: str = ""

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or Path(__file__).resolve().parents[2]
        load_env(root / ".env")
        return cls(
            obs_host=os.environ.get("OBS_HOST", "10.1.10.167"),
            obs_port=int(os.environ.get("OBS_PORT", "4455")),
            obs_password=os.environ.get("OBS_PASSWORD", ""),
            allow_apply=_flag("ALLOW_APPLY", False),
            booth_host=os.environ.get("BOOTH_HOST", "127.0.0.1"),
            booth_port=int(os.environ.get("BOOTH_PORT", "8765")),
            poll_seconds=float(os.environ.get("POLL_SECONDS", "2")),
            runbook_path=root / "runbook" / "church.yaml",
            root=root,
            google_api_key=os.environ.get("GOOGLE_API_KEY", "").strip(),
        )

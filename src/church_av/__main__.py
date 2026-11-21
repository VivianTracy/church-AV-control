from __future__ import annotations

from church_av.config import Settings
from church_av.obs_read import ObsReader
from church_av.runbook import load_runbook
from church_av.server import Booth, serve


def main() -> None:
    settings = Settings.load()
    runbook = load_runbook(settings.runbook_path)
    reader = ObsReader(settings.obs_host, settings.obs_port, settings.obs_password)
    serve(Booth(settings, reader, runbook))


if __name__ == "__main__":
    main()

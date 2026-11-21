from __future__ import annotations

import math
import threading

from church_av.models import InputReading, Snapshot


def _peak_db(levels: object) -> float | None:
    peak = 0.0
    found = False
    if not isinstance(levels, list):
        return None
    for channel in levels:
        if isinstance(channel, (int, float)):
            peak = max(peak, float(channel))
            found = True
        elif isinstance(channel, (list, tuple)):
            for value in channel[:2]:
                if isinstance(value, (int, float)):
                    peak = max(peak, float(value))
                    found = True
    if not found:
        return None
    if peak <= 1e-8:
        return -100.0
    return 20.0 * math.log10(peak)


class ObsReader:
    """Poll OBS on the service machine. The password stays in this process."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 5) -> None:
        self.host = host
        self.port = port
        self._password = password
        self._timeout = timeout
        self._client = None
        self._meters: dict[str, float] = {}
        self._meter_lock = threading.Lock()
        self._meter_client = None
        self._meter_started = False

    def close(self) -> None:
        for client in (self._client, self._meter_client):
            if client is None:
                continue
            try:
                client.disconnect()
            except Exception:
                pass
        self._client = None
        self._meter_client = None

    def read(self) -> Snapshot:
        if not self._password:
            return Snapshot(connected=False, error="OBS_PASSWORD is empty in .env")
        try:
            client = self._request_client()
            self._start_meters()
            version = client.send("GetVersion", raw=True)
            scenes = client.send("GetSceneList", raw=True)
            program = client.send("GetCurrentProgramScene", raw=True)
            inputs = client.send("GetInputList", raw=True)
            stream = client.send("GetStreamStatus", raw=True)
            stats = client.send("GetStats", raw=True)
            readings = [
                self._read_input(client, item)
                for item in inputs.get("inputs", [])
            ]
            names = [item["sceneName"] for item in scenes.get("scenes", [])]
            return Snapshot(
                connected=True,
                obs_version=version.get("obsVersion"),
                platform=version.get("platformDescription") or version.get("platform"),
                program_scene=program.get("currentProgramSceneName"),
                scenes=names,
                streaming=bool(stream.get("outputActive")),
                reconnecting=bool(stream.get("outputReconnecting")),
                output_skipped_frames=int(stream.get("outputSkippedFrames") or 0),
                output_total_frames=int(stream.get("outputTotalFrames") or 0),
                cpu_percent=stats.get("cpuUsage"),
                inputs=readings,
            )
        except Exception as exc:
            self._reset_clients()
            return Snapshot(connected=False, error=str(exc).strip() or exc.__class__.__name__)

    def set_input_mute(self, name: str, muted: bool) -> None:
        self._request_client().send(
            "SetInputMute",
            {"inputName": name, "inputMuted": muted},
            raw=True,
        )

    def set_input_volume_db(self, name: str, volume_db: float) -> None:
        self._request_client().send(
            "SetInputVolume",
            {"inputName": name, "inputVolumeDb": volume_db},
            raw=True,
        )

    def set_program_scene(self, name: str) -> None:
        self._request_client().send(
            "SetCurrentProgramScene",
            {"sceneName": name},
            raw=True,
        )

    def _request_client(self):
        if self._client is None:
            import obsws_python as obs

            self._client = obs.ReqClient(
                host=self.host,
                port=self.port,
                password=self._password,
                timeout=self._timeout,
            )
        return self._client

    def _reset_clients(self) -> None:
        self.close()
        self._meter_started = False

    def _start_meters(self) -> None:
        if self._meter_started:
            return
        self._meter_started = True
        try:
            import obsws_python as obs

            def on_input_volume_meters(data) -> None:
                updates: dict[str, float] = {}
                for item in getattr(data, "inputs", None) or []:
                    if not isinstance(item, dict):
                        continue
                    level = _peak_db(item.get("inputLevelsMul"))
                    name = item.get("inputName")
                    if name and level is not None:
                        updates[name] = level
                if updates:
                    with self._meter_lock:
                        self._meters.update(updates)

            self._meter_client = obs.EventClient(
                host=self.host,
                port=self.port,
                password=self._password,
                subs=obs.Subs.LOW_VOLUME | obs.Subs.INPUTVOLUMEMETERS,
                timeout=self._timeout,
            )
            self._meter_client.callback.register(on_input_volume_meters)
        except Exception:
            self._meter_client = None

    def _read_input(self, client, item: dict) -> InputReading:
        name = item.get("inputName", "")
        kind = item.get("inputKind", "")
        reading = InputReading(name=name, kind=kind)
        if kind in {"monitor_capture"}:
            return reading
        try:
            mute = client.send("GetInputMute", {"inputName": name}, raw=True)
            volume = client.send("GetInputVolume", {"inputName": name}, raw=True)
            reading.muted = bool(mute.get("inputMuted"))
            reading.volume_db = float(volume.get("inputVolumeDb"))
        except Exception:
            reading.muted = None
            reading.volume_db = None
        with self._meter_lock:
            reading.meter_db = self._meters.get(name)
        return reading

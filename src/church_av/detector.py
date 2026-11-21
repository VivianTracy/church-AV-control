from __future__ import annotations

from dataclasses import dataclass, field

from church_av.models import Action, Fault, Snapshot
from church_av.runbook import Runbook


@dataclass
class Detector:
    """Turn an OBS snapshot into faults. This does not call a model."""

    _previous_skipped: int | None = None
    _previous_total: int | None = None
    _clip_streak: int = 0
    _silence_streak: int = 0
    _dismissed: set[str] = field(default_factory=set)

    def dismiss(self, fault_id: str) -> None:
        self._dismissed.add(fault_id)

    def detect(self, snapshot: Snapshot, runbook: Runbook) -> list[Fault]:
        faults: list[Fault] = []
        if not snapshot.connected:
            faults.append(
                Fault(
                    id="obs_disconnected",
                    severity="critical",
                    title="Cannot read the service machine",
                    evidence=snapshot.error or "OBS WebSocket did not answer.",
                    steps=[
                        "On the service PC, confirm OBS is open.",
                        "In Tools → WebSocket Server Settings, Enable WebSocket server stays checked.",
                        "Windows Security → Firewall → Allow an app, OBS Studio, Private is checked.",
                    ],
                )
            )
            return self._visible(faults)

        mix = runbook.house_mix
        house = snapshot.input_named(mix.obs_name)
        if house is None:
            faults.append(
                Fault(
                    id="house_mix_missing",
                    severity="critical",
                    title=f"OBS has no input named {mix.obs_name}",
                    evidence="The runbook expects the Behringer X32 USB mix under that name.",
                    steps=[
                        "In OBS Sources, find the input that is Microphone (X-USB).",
                        f"Rename it to {mix.obs_name}, or update runbook/church.yaml.",
                    ],
                )
            )
        elif house.muted:
            faults.append(
                Fault(
                    id="house_mix_muted",
                    severity="critical",
                    title="House mix is muted in OBS",
                    evidence=(
                        f"{mix.obs_name} ({mix.windows_device}) is muted. "
                        "Viewers would hear silence even if the board meters are moving."
                    ),
                    steps=[
                        "In the OBS Audio Mixer, click the speaker on Audio Input Capture.",
                        "Leave Wall Cam, Mobile Cam, and Slide PC muted. Their audio is off on purpose.",
                    ],
                    action=Action(
                        kind="set_input_mute",
                        summary=f"Unmute {mix.obs_name}",
                        input_name=mix.obs_name,
                        muted=False,
                    ),
                )
            )
        elif house.volume_db is not None and house.volume_db <= mix.warn_below_db:
            faults.append(
                Fault(
                    id="house_mix_fader_low",
                    severity="warning",
                    title="House mix fader is down in OBS",
                    evidence=(
                        f"{mix.obs_name} is at {house.volume_db:.0f} dB. "
                        f"The usual position is about {mix.target_db:.0f} dB."
                    ),
                    steps=[
                        "In the OBS Audio Mixer, raise Audio Input Capture.",
                        runbook.board_instruction(
                            "main",
                            "If the board meters are already low, raise {name} on the {board}.",
                        ),
                    ],
                )
            )

        if house is not None and not house.muted and house.meter_db is not None:
            if house.meter_db >= -1:
                self._clip_streak += 1
            else:
                self._clip_streak = 0
            if house.meter_db <= -55:
                self._silence_streak += 1
            else:
                self._silence_streak = 0
            if self._clip_streak >= 2:
                faults.append(
                    Fault(
                        id="house_mix_clipping",
                        severity="warning",
                        title="House mix is clipping",
                        evidence=f"The X-USB meter is peaking near full scale ({house.meter_db:.1f} dB).",
                        steps=[
                            runbook.board_instruction(
                                "main",
                                "On the {board}, lower {name} a little.",
                            ),
                            "OBS should stay near its current fader. The clip is coming from the board.",
                        ],
                    )
                )
            if runbook.expect_stream_live and self._silence_streak >= 3:
                faults.append(
                    Fault(
                        id="house_mix_silent",
                        severity="critical",
                        title="House mix is silent",
                        evidence="Audio Input Capture is open, and the X-USB meter has been at the noise floor.",
                        steps=[
                            runbook.board_instruction(
                                "main",
                                "Check that the {board} USB send is up and {name} is not muted.",
                            ),
                            "In OBS, Audio Input Capture should stay unmuted.",
                        ],
                    )
                )
        else:
            self._clip_streak = 0
            self._silence_streak = 0

        if snapshot.reconnecting:
            faults.append(
                Fault(
                    id="stream_reconnecting",
                    severity="warning",
                    title="YouTube connection is reconnecting",
                    evidence="OBS reports the stream output reconnecting.",
                    steps=["Check the service PC's internet before restarting the stream."],
                )
            )
        elif runbook.expect_stream_live and not snapshot.streaming:
            faults.append(
                Fault(
                    id="stream_stopped",
                    severity="critical",
                    title="The stream is not live",
                    evidence="The runbook expects YouTube to be live, and OBS output is stopped.",
                    steps=[
                        "On the service PC, click Start Streaming in OBS.",
                        "This booth will not start the stream for you.",
                    ],
                )
            )

        if snapshot.streaming:
            skipped = self._skipped_ratio(snapshot)
            if skipped is not None and skipped >= 0.05:
                faults.append(
                    Fault(
                        id="frames_skipped",
                        severity="warning",
                        title="OBS is dropping outgoing frames",
                        evidence=f"{skipped:.0%} of recent output frames were skipped.",
                        steps=[
                            "Close other apps on the service PC.",
                            "If it continues, lower the OBS output resolution.",
                        ],
                    )
                )
        else:
            self._previous_skipped = None
            self._previous_total = None

        return self._visible(faults)

    def _skipped_ratio(self, snapshot: Snapshot) -> float | None:
        previous_skipped = self._previous_skipped
        previous_total = self._previous_total
        self._previous_skipped = snapshot.output_skipped_frames
        self._previous_total = snapshot.output_total_frames
        if previous_skipped is None or previous_total is None:
            return None
        delta_total = snapshot.output_total_frames - previous_total
        delta_skipped = snapshot.output_skipped_frames - previous_skipped
        if delta_total < 30 or delta_skipped < 0:
            return None
        return delta_skipped / delta_total

    def _visible(self, faults: list[Fault]) -> list[Fault]:
        return [fault for fault in faults if fault.id not in self._dismissed]

# ADK and A2UI plan

The operator computer runs the booth. The service PC runs OBS and sends YouTube. Build and test the agent here, off the church network, against saved snapshots from the 20 Nov 2026 rehearsal.

## Two clocks

The monitor runs every 2 seconds and never calls a model. `ObsReader` polls OBS WebSocket for the program scene, stream status, skipped frames, and the Audio Input Capture mute, fader, and meter. `Detector` turns that reading plus `runbook/church.yaml` into a fault. If Gemini is down, this loop still runs.

ADK runs only when a new fault opens, or when the operator sends a chat message. The agent reads the current snapshot and the runbook. It returns a short reply and a guide. It has no tool that writes to OBS.

## Safe adjustments

A policy module is the only writer. The agent may name an action. The policy drops anything outside this list. `ALLOW_APPLY` stays false until the list is tested on demo snapshots.

| Adjustment | Where | Limit |
|---|---|---|
| Unmute Audio Input Capture | OBS Audio Mixer | House mix only. Camera inputs stay muted. |
| Nudge the house-mix fader | OBS Audio Mixer | At most 3 dB per tap, never above 0 dB, never a mute. |
| Change the program scene | OBS Scenes | Only after a chat confirmation, and only to a scene name OBS just reported. |
| One mic or the board main mix | Behringer X32 | Shown on a guide card. The X-USB mix has no remote control. The channel name must already be in the runbook. |

Starting or stopping the stream, clicking Go Live, changing the stream key, and deleting a source are not actions.

## Booth screen

One A2UI surface, basic catalog:

- Status, already built: scene, YouTube output, house-mix line, fault cards.
- Chat: a `TextField` and an Ask `Button`. The reply is two or three sentences.
- Guide card: the place in large type (`OBS Audio Mixer` or `Behringer X32`), the control name (`Audio Input Capture`), then the steps. An Apply button is included only when the policy accepted an action and `ALLOW_APPLY` is true.

An unknown board channel becomes a question on that card. The model does not invent a knob name.

## Build order

1. Save demo snapshots: healthy, muted house mix, low fader, live stream, dropped frames.
2. Add `policy.py` and tests that refuse stream start, a house-mix mute, and a fader move above 0 dB.
3. Add an ADK agent whose tools are read-only: snapshot, runbook, open faults. A missing `GOOGLE_API_KEY` uses the detector text.
4. Add the chat field and the guide card to the A2UI page. Ask about a muted mix and see the speaker-icon steps.
5. Add a board-channel section to `runbook/church.yaml`. Empty names ask the operator instead of guessing.

## Deploy

On the church network, the same program on the operator laptop connects to OBS on the service PC. The service PC does not run the agent. Gemini is called for a new fault or a chat message only.

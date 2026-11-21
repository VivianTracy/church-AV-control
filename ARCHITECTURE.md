# ADK and A2UI plan

The operator computer runs the booth. The service PC runs OBS and sends YouTube. Build and test the agent here, off the church network, against saved snapshots from the 20 Nov 2026 rehearsal.

## Two connections

OBS on the service PC receives one combined feed. Windows calls it Microphone (X-USB). That is the only audio input the stream uses. Pulpit, handheld, playback, and the house mix are not separate OBS sources.

The Behringer X32 may be monitored separately over OSC (UDP 10023). `X32Reader` sits beside `ObsReader` and is read-only. It collects channel, bus, matrix, and main-output meters, plus scribble names. It never sends a fader, a mute, or a name. An empty `X32_HOST` leaves this connection off. OBS polling still runs.

The monitor runs every 2 seconds and never calls a model. `ObsReader` polls OBS WebSocket for the program scene, stream status, skipped frames, and the Audio Input Capture mute, fader, and meter. `X32Reader`, when configured, polls the mixer on the same tick. `Detector` turns those readings plus `runbook/church.yaml` into a fault. If Gemini is down, this loop still runs.

ADK runs only when a new fault opens, or when the operator sends a chat message. The agent reads the current snapshot and the runbook. It returns a short diagnosis and a guide. It has no tool that writes to OBS or to the X32. A deterministic policy decides which operator-approved OBS changes are allowed.

## Signal path

When the X-USB meter is at the noise floor during a live service, compare four stages:

1. Mic channel on the X32.
2. Stream bus, if one is assigned.
3. X32 USB output. Today that is Main L/R, until a bus or matrix is routed to the USB card.
4. OBS Audio Input Capture.

A quiet mic means the mic is not reaching the mixer. A hot mic and a quiet stream bus means the channel is not in the stream mix. A hot USB send and a quiet OBS input means the mixer audio is not reaching OBS. A quiet mic while the OBS meter is moving is not a fault.

## Stream mix

The room can stay on Main L/R. YouTube can take a bus or a matrix instead, and that mix is what the USB card sends. Until `stream_mix.strip` is set, the stream uses the house mix, and OBS still sees one X-USB input. Assigning that bus is the useful mixer change for the stream. The booth will not route it. The operator does that on the console, then records the strip in the runbook.

## Names

Scribble names read off channels 1–16 are under `board.labels`. Channels 1–9 are Worship. Then Keys L, Piano L, Piano R, Offering, Moderator on 14, Reception on 15, and Speaker on 16. Channels 17–32 are not readable in the photos, so the pulpit mic has no strip.

Handheld, playback, and the pulpit mic have no strip yet. If the console sends a real name over OSC, that name wins. A factory `Ch17` label is ignored. Otherwise the operator enters the strip, with `map_channel` or by editing `strip` in the runbook. The booth does not invent a channel number.

## Safe adjustments

A policy module is the only writer. The agent may name an action. The policy drops anything outside this list. `ALLOW_APPLY` stays false until the list is tested on demo snapshots.

| Adjustment | Where | Limit |
|---|---|---|
| Unmute Audio Input Capture | OBS Audio Mixer | House mix only. Camera inputs stay muted. |
| Nudge the house-mix fader | OBS Audio Mixer | At most 3 dB per tap, never above 0 dB, never a mute. |
| Change the program scene | OBS Scenes | Only after a chat confirmation, and only to a scene name OBS just reported. |
| Map a runbook role to a strip | runbook/church.yaml | Operator action only. A channel, bus, or matrix number. Not a mixer write. |
| One mic, the stream bus, or the main mix | Behringer X32 | Shown on a guide card. OSC does not move the fader. |

Starting or stopping the stream, clicking Go Live, changing the stream key, deleting a source, and any X32 fader or mute are not actions.

## What is not monitored

The ADJ PC-100A has physical power switches. The amplifiers, wireless receivers, and lighting controller did not show a control interface. This program does not poll them. A reading would need a protocol or a sensor that is not in the rack today.

## Booth screen

One A2UI surface, basic catalog:

- Status, already built: scene, YouTube output, house-mix line, fault cards. When OSC is connected, the house-mix line also shows the mic, stream, USB, and OBS stages.
- Chat: a `TextField` and an Ask `Button`. The reply is two or three sentences.
- Guide card: the place in large type (`OBS Audio Mixer` or `Behringer X32`), the control name (`Audio Input Capture` or the console name), then the steps. An Apply button is included only when the policy accepted an action and `ALLOW_APPLY` is true.

An unknown board channel becomes a question on that card. The model does not invent a knob name.

## Build order

1. Save demo snapshots: healthy, muted house mix, low fader, live stream, dropped frames.
2. Add `policy.py` and tests that refuse stream start, a house-mix mute, and a fader move above 0 dB.
3. Add an ADK agent whose tools are read-only: snapshot, runbook, open faults. A missing `GOOGLE_API_KEY` uses the detector text.
4. Add the chat field and the guide card to the A2UI page. Ask about a muted mix and see the speaker-icon steps.
5. Add a board-channel section to `runbook/church.yaml`. Empty names ask the operator instead of guessing.
6. Add `X32Reader` for channel, bus, and output meters. Keep it read-only. Let the detector name the stage that went quiet.

## Deploy

On the church network, the same program on the operator laptop connects to OBS on the service PC and, when `X32_HOST` is set, to the X32 over OSC. The service PC does not run the agent. Gemini is called for a new fault or a chat message only.

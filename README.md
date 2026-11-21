# Church stream booth

This computer watches the service PC. OBS on that PC stays the thing that streams to YouTube. Nothing here starts or stops the stream.

## What it reads

OBS WebSocket on the service PC (`10.1.10.167:4455`) reports:

- the program scene
- whether the YouTube output is live
- the Audio Mixer input, `Audio Input Capture`, which Windows calls Microphone (X-USB)

That device is one combined Behringer X32 mix. Individual mics are not separate OBS inputs. Wall Cam, Mobile Cam, and Slide PC stay muted on purpose.

The X32 can also be watched on its own, over OSC. Set `X32_HOST` in `.env` (port `10023`). That connection is read-only: channel, bus, matrix, and main meters, plus the names on the console. It does not move a fader. Leave `X32_HOST` empty and the booth keeps watching OBS only.

Scribble names read off the console are in `runbook/church.yaml`:

- channels 1–9: Worship
- 10 Keys L, 11 Piano L, 12 Piano R, 13 Offering
- 14 Moderator, 15 Reception, 16 Speaker

Channels 17–32 are not readable in the photos, so the pulpit mic has no strip yet. Handheld and playback still need a number too. The booth asks, and does not invent one. A name the console sends over OSC replaces the photo label. A factory label such as `Ch17` does not.

YouTube can use its own bus or matrix instead of the house Main L/R. Leave `stream_mix.strip` empty until that bus is routed to the USB card. OBS still receives whichever mix the card sends, as the one X-USB input.

The ADJ PC-100A has physical power switches. The amplifiers, wireless receivers, and lighting controller have no reading here.

The meanings of the scenes live in `runbook/church.yaml`. When OBS shows a scene that file does not describe, the booth says so.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
cp .env.example .env
```

Put the OBS WebSocket password in `.env`. Then:

```bash
python -m church_av
```

Open http://127.0.0.1:8765

`ALLOW_APPLY` stays `false`. The booth suggests the OBS click and does not change the service PC. Set it to `true` only when an Apply button should be allowed to unmute the Audio Mixer. Apply never writes to the X32. The policy decides which operator-approved OBS changes are allowed. Mapping a strip edits `runbook/church.yaml` only.

Set `expect_stream_live: true` in `runbook/church.yaml` when a Sunday service should already be on YouTube. Until then, a stopped stream is normal.

# Church stream booth

This computer watches the service PC. OBS on that PC stays the thing that streams to YouTube. Nothing here starts or stops the stream.

## What it reads

OBS WebSocket on the service PC (`10.1.10.167:4455`) reports:

- the program scene
- whether the YouTube output is live
- the one house-mix input, `Audio Input Capture`, which Windows calls Microphone (X-USB)

That device is the Behringer mix. Individual mics are not separate OBS inputs. Wall Cam, Mobile Cam, and Slide PC stay muted on purpose.

The physical channel labels live under `board` in `runbook/church.yaml`. Leave `name` empty until someone reads it off the mixer. The booth then asks which knob, and does not invent a number.

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

`ALLOW_APPLY` stays `false`. The booth suggests the OBS click and does not change the service PC. Set it to `true` only when an Apply button should be allowed to unmute the house mix.

Set `expect_stream_live: true` in `runbook/church.yaml` when a Sunday service should already be on YouTube. Until then, a stopped stream is normal.

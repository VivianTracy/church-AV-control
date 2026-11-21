# Architecture

This program runs on the operator's computer. The service PC runs OBS and sends the YouTube stream.

```text
Service PC (10.1.10.167)
  Behringer X-USB  ->  OBS "Audio Input Capture"
  Wall Cam, Mobile Cam, Slide PC, Acer display
  OBS WebSocket :4455
        |
        |  scenes, mute, fader, meters, stream status
        v
This computer
  ObsReader        polls OBS and keeps the latest meter
  Detector         decides faults from the runbook
  Booth page       draws A2UI 1.0 messages
  Apply            unmute of the house mix, only if ALLOW_APPLY=true
```

The detector is ordinary code. It runs every poll, including when the network to a model is down. A later ADK agent can phrase a fault for a volunteer. It does not get a tool that starts or stops the stream, and it does not choose a different fix than the detector.

A2UI is the booth screen. Each poll sends one `createSurface` message using the basic catalog (`Column`, `Text`, `Card`, `Button`). The page renders that catalog. An Apply click posts an A2UI `action` message back to this computer. With `ALLOW_APPLY=false`, that action is refused.

BlackHole is not on this path. The service PC is Windows, and the mix arrives as the Behringer USB device.

Learning the room means keeping `runbook/church.yaml` next to the live OBS inventory. The file records what each source is. The live read records what OBS is doing now. A scene OBS has and the file does not mention is shown as undescribed, which is the prompt to add one line.

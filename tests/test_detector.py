from church_av.detector import Detector
from church_av.models import InputReading, Snapshot
from church_av.runbook import HouseMix, Runbook


def runbook(**overrides) -> Runbook:
    mix = HouseMix(
        obs_name="Audio Input Capture",
        windows_device="Microphone (X-USB)",
        role="Behringer USB mix",
        target_db=-2,
        warn_below_db=-20,
    )
    base = dict(
        church="Church stream",
        service_name="Sunday service",
        expect_stream_live=False,
        house_mix=mix,
    )
    base.update(overrides)
    return Runbook(**base)


def snapshot(**overrides) -> Snapshot:
    house = InputReading(
        name="Audio Input Capture",
        kind="wasapi_input_capture",
        muted=False,
        volume_db=-1.8,
        meter_db=-18,
    )
    base = dict(
        connected=True,
        program_scene="2-Slide Only",
        scenes=["2-Slide Only", "1-Stage Only"],
        streaming=False,
        inputs=[
            house,
            InputReading(name="Wall Cam", muted=True, volume_db=-100),
        ],
    )
    base.update(overrides)
    return Snapshot(**base)


def codes(faults) -> set[str]:
    return {fault.id for fault in faults}


def test_quiet_friday_has_no_faults():
    assert codes(Detector().detect(snapshot(), runbook())) == set()


def test_muted_house_mix_can_be_unmuted():
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=True, volume_db=-1.8)]
    )
    faults = Detector().detect(reading, runbook())
    assert codes(faults) == {"house_mix_muted"}
    assert faults[0].action is not None
    assert faults[0].action.muted is False
    assert "Wall Cam" in " ".join(faults[0].steps)


def test_camera_mute_is_not_a_fault():
    reading = snapshot(
        inputs=[
            InputReading(name="Audio Input Capture", muted=False, volume_db=-1.8),
            InputReading(name="Mobile Cam", muted=True, volume_db=-100),
        ]
    )
    assert codes(Detector().detect(reading, runbook())) == set()


def test_low_fader_warns():
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=False, volume_db=-35)]
    )
    assert codes(Detector().detect(reading, runbook())) == {"house_mix_fader_low"}


def test_stopped_stream_is_quiet_until_sunday_mode():
    stopped = snapshot(streaming=False)
    assert "stream_stopped" not in codes(Detector().detect(stopped, runbook()))
    live = Detector().detect(stopped, runbook(expect_stream_live=True))
    assert codes(live) == {"stream_stopped"}
    assert live[0].action is None


def test_disconnect_hides_other_faults():
    faults = Detector().detect(Snapshot(connected=False, error="timed out"), runbook())
    assert codes(faults) == {"obs_disconnected"}


def test_clip_needs_two_readings_and_silence_needs_sunday_mode():
    loud = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=False, volume_db=-1.8, meter_db=-0.2)]
    )
    detector = Detector()
    assert "house_mix_clipping" not in codes(detector.detect(loud, runbook()))
    assert "house_mix_clipping" in codes(detector.detect(loud, runbook()))

    quiet = Detector()
    silent = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=False, volume_db=-1.8, meter_db=-80)]
    )
    for _ in range(3):
        assert "house_mix_silent" not in codes(quiet.detect(silent, runbook()))
    sunday = Detector()
    book = runbook(expect_stream_live=True)
    sunday.detect(silent, book)
    sunday.detect(silent, book)
    assert "house_mix_silent" in codes(sunday.detect(silent, book))


def test_dismiss_hides_a_fault_until_a_new_detector():
    reading = snapshot(
        inputs=[InputReading(name="Audio Input Capture", muted=True, volume_db=-1.8)]
    )
    detector = Detector()
    assert codes(detector.detect(reading, runbook())) == {"house_mix_muted"}
    detector.dismiss("house_mix_muted")
    assert detector.detect(reading, runbook()) == []

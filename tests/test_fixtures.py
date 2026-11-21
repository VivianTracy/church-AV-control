from church_av.detector import Detector
from church_av.fixtures import FIXTURE_NAMES, load_fixture, load_fixture_record
from church_av.runbook import load_runbook
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rehearsal_fixtures_cover_the_five_cases():
    assert FIXTURE_NAMES == (
        "healthy",
        "muted_house_mix",
        "low_fader",
        "live_stream",
        "dropped_frames",
        "clipping",
    )


def test_each_fixture_produces_the_recorded_faults():
    runbook = load_runbook(ROOT / "runbook" / "church.yaml")
    for name in FIXTURE_NAMES:
        record = load_fixture_record(name)
        expected_sets = record.get("expect_faults_each")
        if expected_sets is None:
            expected_sets = [record["expect_faults"]]
        detector = Detector()
        snapshots = load_fixture(name)
        assert len(snapshots) == len(expected_sets), name
        for snapshot, expected in zip(snapshots, expected_sets):
            found = [fault.id for fault in detector.detect(snapshot, runbook)]
            assert found == expected, name

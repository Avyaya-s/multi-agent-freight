from datetime import datetime
from pathlib import Path

from freight_sim.events import EventLog, EventType, SCHEMA_VERSION, read_events
from freight_sim.models import Location


def test_event_log_round_trips_and_assigns_monotonic_seq(tmp_path: Path):
    log_path = tmp_path / "run.jsonl"
    log = EventLog(log_path, run_id="test-run")
    log.emit(datetime(2024, 1, 1, 8), EventType.SIM_STARTED, "sim", {"seed": 42})
    log.emit(
        datetime(2024, 1, 1, 9),
        EventType.TRUCK_SPAWNED,
        "truck-001",
        {"home_location": Location(lat=1.0, lon=2.0), "capacity_weight_kg": 5000.0},
    )
    log.close()

    events = list(read_events(log_path))
    assert len(events) == 2
    assert [e.seq for e in events] == [0, 1]
    assert all(e.run_id == "test-run" for e in events)
    assert all(e.schema_version == SCHEMA_VERSION for e in events)
    assert events[0].event_type == EventType.SIM_STARTED
    assert events[1].payload["home_location"] == {"lat": 1.0, "lon": 2.0, "node_id": None}


def test_seq_is_unique_even_when_sim_time_ties(tmp_path: Path):
    log_path = tmp_path / "run.jsonl"
    log = EventLog(log_path, run_id="test-run")
    same_time = datetime(2024, 1, 1, 8)
    for i in range(5):
        log.emit(same_time, EventType.TRUCK_WAIT_STARTED, f"truck-{i}", {})
    log.close()

    events = list(read_events(log_path))
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]
    assert all(e.sim_time == same_time for e in events)

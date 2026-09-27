"""The event log: the single source of truth for the simulation.

Every state change is an event. Metrics (metrics.py) and the future replay
viewer (Stage 3) read only this log, never live simulator state. Because the
log is the simulator's own omniscient record -- not a channel between agents --
it is fine (and necessary) for it to carry private fields such as a truck's
cost structure: TRUCK_SPAWNED must carry enough to reconstruct fuel and cost
metrics without holding a reference to a live Truck object.

Each event gets a monotonically increasing `seq` (multiple events can share a
`sim_time`, and the viewer needs a deterministic order) and a `schema_version`,
so later stages can replay Stage 0 logs without guessing what a field meant.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from freight_sim.models import Location

SCHEMA_VERSION = "1.0.0"


class EventType(str, Enum):
    SIM_STARTED = "sim_started"
    SIM_ENDED = "sim_ended"

    TRUCK_SPAWNED = "truck_spawned"

    LOAD_POSTED = "load_posted"
    LOAD_EXPIRED = "load_expired"

    MATCH_PROPOSED = "match_proposed"
    MATCH_VERIFIED = "match_verified"
    MATCH_VETOED = "match_vetoed"

    DEAL_CREATED = "deal_created"
    DEAL_COMPLETED = "deal_completed"
    DEAL_FAILED = "deal_failed"

    TRUCK_DEPARTED = "truck_departed"
    TRUCK_ARRIVED = "truck_arrived"

    TRUCK_WAIT_STARTED = "truck_wait_started"
    TRUCK_WAIT_ENDED = "truck_wait_ended"

    TRUCK_IDLE_STARTED = "truck_idle_started"
    TRUCK_IDLE_ENDED = "truck_idle_ended"


class DeparturePurpose(str, Enum):
    DELIVERY = "delivery"  # laden, carrying a matched load
    REPOSITION_TO_PICKUP = "reposition_to_pickup"  # empty, heading to a matched load's origin
    DEADHEAD_HOME = "deadhead_home"  # empty, giving up / heading home
    SPECULATIVE_REPOSITION = "speculative_reposition"  # reserved for a later stage; not emitted yet


class DeadheadReason(str, Enum):
    GAVE_UP = "gave_up"  # waited past max_wait_hours with no match
    HOME_DEADLINE = "home_deadline"  # home_by is approaching
    NO_LOADS_POSTED = "no_loads_posted"  # nothing open matched at all, decided immediately


class VetoReason(str, Enum):
    # PHYSICAL layer (feasibility.check_feasibility): public/platform data only.
    TIME_WINDOW = "time_window"
    CAPACITY = "capacity"
    # PREFERENCE layer (feasibility.check_preference): TruckPrivate data.
    # Stage 1's deterministic stand-in for the trucker agent; Stage 2 puts an
    # LLM at this same boundary.
    DETOUR_LIMIT = "detour_limit"
    HOME_DEADLINE = "home_deadline"
    RATE_TOO_LOW = "rate_too_low"


class FeasibilityLayer(str, Enum):
    """Which layer rejected (or would have decided) a match. Logged on every
    veto so "physically impossible" and "owner wouldn't accept" are reported
    as separate findings, never pooled into one veto rate."""

    PHYSICAL = "physical"
    PREFERENCE = "preference"


class DealFailureReason(str, Enum):
    VETOED = "vetoed"
    NO_SHOW = "no_show"
    BREAKDOWN = "breakdown"
    WINDOW_MISSED = "window_missed"
    TRUCK_UNAVAILABLE = "truck_unavailable"


@dataclass
class Event:
    event_id: str
    run_id: str
    seq: int
    schema_version: str
    sim_time: datetime
    event_type: EventType
    entity_id: str
    payload: dict[str, Any]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Location):
        return {"lat": obj.lat, "lon": obj.lon, "node_id": obj.node_id}
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Not JSON serializable: {obj!r}")


def event_to_json(event: Event) -> str:
    row = asdict(event)
    return json.dumps(row, default=_json_default, sort_keys=True)


def event_from_json(line: str) -> Event:
    row = json.loads(line)
    return Event(
        event_id=row["event_id"],
        run_id=row["run_id"],
        seq=row["seq"],
        schema_version=row["schema_version"],
        sim_time=datetime.fromisoformat(row["sim_time"]),
        event_type=EventType(row["event_type"]),
        entity_id=row["entity_id"],
        payload=row["payload"],
    )


class EventLog:
    """Appends events to a JSONL file, one run per file.

    Assigns `seq` itself so callers never need to track ordering by hand.
    """

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(self, sim_time: datetime, event_type: EventType, entity_id: str, payload: dict[str, Any]) -> Event:
        event = Event(
            event_id=str(uuid.uuid4()),
            run_id=self.run_id,
            seq=self._seq,
            schema_version=SCHEMA_VERSION,
            sim_time=sim_time,
            event_type=event_type,
            entity_id=entity_id,
            payload=payload,
        )
        self._seq += 1
        self._fh.write(event_to_json(event) + "\n")
        self._fh.flush()
        return event

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_events(path: Path) -> Iterator[Event]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield event_from_json(line)

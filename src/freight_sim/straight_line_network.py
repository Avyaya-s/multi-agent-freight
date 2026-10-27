"""Approximate network for interactive mode -- haversine distance instead of
real OSM routing. Decided with the user after timing a live session on the
real RoadNetwork: ~30s to load the cached graph plus ~2s per never-before-
seen node pair, which measured out to ~40s of pure screening dead time
before a single negotiation message appears. Visitors can drop a pin
anywhere, so cache-warming can't help -- every session's coordinates are new.

Batch mode keeps RoadNetwork (network.py) unchanged: its whole point is
structural realism against real roads, and its results (the README's
numbers) must not depend on this trade-off. Interactive mode trades that
realism for a live-feeling demo -- detour/ETA figures shown to a visitor are
straight-line estimates, not real-road distances (the project's own earlier
measurement found real roads run ~1.3-1.9x straight-line distance here, so
this is a known, named approximation, not a hidden one).

Same shape as RoadNetwork (see Network protocol below), so every module that
takes a network -- feasibility.py, matching.py, demand.py, session.py --
works unmodified. This was already used ad hoc, duplicated, in
scripts/run_one_negotiation.py and check_negotiation_quality.py before
interactive mode needed it for real; this is the one shared implementation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from freight_sim.geo import haversine_km
from freight_sim.models import Location


class Network(Protocol):
    """What feasibility.py/matching.py/demand.py actually require from a
    network -- satisfied structurally by both RoadNetwork and
    StraightLineNetwork, no inheritance needed."""

    def locate(self, lat: float, lon: float) -> Location: ...
    def distance_km(self, origin: Location, destination: Location) -> float: ...
    def travel_time_min(self, origin: Location, destination: Location, depart_time: datetime) -> float: ...


class StraightLineNetwork:
    SPEED_KMPH = 30.0

    def locate(self, lat: float, lon: float) -> Location:
        return Location(lat=lat, lon=lon, node_id=None)

    def distance_km(self, origin: Location, destination: Location) -> float:
        return haversine_km(origin, destination)

    def travel_time_min(self, origin: Location, destination: Location, depart_time: datetime) -> float:
        return (self.distance_km(origin, destination) / self.SPEED_KMPH) * 60.0

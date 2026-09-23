from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from freight_sim.config import Config, load_config
from freight_sim.geo import haversine_km
from freight_sim.models import Location

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeNetwork:
    """Straight-line stand-in for RoadNetwork. Matching and simulator code only
    ever call locate()/distance_km()/travel_time_min(), so this is a drop-in
    replacement that lets tests run in milliseconds with no OSM data."""

    SPEED_KMPH = 30.0

    def locate(self, lat: float, lon: float) -> Location:
        return Location(lat=lat, lon=lon, node_id=None)

    def distance_km(self, origin: Location, destination: Location) -> float:
        return haversine_km(origin, destination)

    def travel_time_min(self, origin: Location, destination: Location, depart_time: datetime) -> float:
        return (self.distance_km(origin, destination) / self.SPEED_KMPH) * 60.0


@pytest.fixture
def fake_network() -> FakeNetwork:
    return FakeNetwork()


@pytest.fixture
def small_config() -> Config:
    config, _ = load_config(REPO_ROOT / "config" / "default.yaml")
    config.run.sim_days = 2
    config.fleet.num_trucks = 6
    return config

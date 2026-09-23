"""Loads config/*.yaml into validated, typed config objects.

Nothing else in this package should read a YAML file directly, and no tunable
number should be hardcoded outside these files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel


class Range(BaseModel):
    min: float
    max: float


class RunConfig(BaseModel):
    seed: int
    sim_days: int


class BoundingBox(BaseModel):
    north: float
    south: float
    east: float
    west: float


class NetworkConfig(BaseModel):
    # A bounding box, not a place name: the industrial region this project
    # models (Peenya / Bommasandra / Hosur / Whitefield) spans a Karnataka-
    # Tamil Nadu state border, so a Nominatim place lookup would only return
    # one locality's boundary rather than the whole area.
    bbox: BoundingBox
    cache_path: str
    network_type: str
    travel_time_hourly_multipliers: dict[int, float]


class FleetConfig(BaseModel):
    num_trucks: int
    capacity_weight_kg: Range
    capacity_volume_m3: Range
    fixed_cost_per_day: Range
    variable_cost_per_km: Range
    fuel_efficiency_kmpl: Range
    variable_cost_per_hour_waiting: Range
    reservation_rate_per_km: Range
    return_leg_discount: Range
    max_detour_km: Range
    max_wait_hours: Range
    home_deadline_probability: float


class ZoneConfig(BaseModel):
    name: str
    lat: float
    lon: float
    outbound_loads_per_day: Range
    inbound_loads_per_day: Range


class DemandConfig(BaseModel):
    zones: list[ZoneConfig]
    weight_kg: Range
    volume_m3: Range
    posted_rate_per_km: Range
    reservation_rate_per_km_markup: Range
    penalty_per_hour_late: Range
    loading_time_min: Range
    unloading_time_min: Range
    pickup_window_hours: float
    delivery_slack_hours: Range


class MatchingConfig(BaseModel):
    broker_commission_rate: float
    max_candidate_detour_km: float
    retry_interval_hours: float
    home_deadline_buffer_hours: float


class Config(BaseModel):
    run: RunConfig
    network: NetworkConfig
    fleet: FleetConfig
    demand: DemandConfig
    matching: MatchingConfig


def config_hash(raw_text: str) -> str:
    """Hash of the exact config file contents, so a run's results can be
    traced back to the exact assumptions that produced them."""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path) -> tuple[Config, str]:
    """Returns (parsed config, hash of the raw file)."""
    raw_text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    return Config.model_validate(data), config_hash(raw_text)

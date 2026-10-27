"""The marketplace's coarse candidate screen.

propose_candidates() is what the marketplace can do: it only ever sees public
Load terms and a truck's *platform* tier (location, status, spare capacity).
It has no access to a truck's private reservation price, max detour or home
deadline, so it can only screen coarsely (capacity fit + a calibrated
straight-line distance cap from config -- see README's veto-rate diagnosis
for how that cap was chosen). It stays a screen, not a decision: real
feasibility is decided downstream in feasibility.py's two-layer check.
"""

from __future__ import annotations

from freight_sim.config import MatchingConfig
from freight_sim.geo import haversine_km
from freight_sim.models import Load, Truck


def propose_candidates(truck: Truck, open_loads: list[Load], matching_cfg: MatchingConfig) -> list[Load]:
    candidates = [
        load
        for load in open_loads
        if load.public.weight_kg <= truck.platform.spare_capacity_weight_kg
        and load.public.volume_m3 <= truck.platform.spare_capacity_volume_m3
        and haversine_km(truck.platform.current_location, load.public.origin) <= matching_cfg.max_candidate_detour_km
    ]
    candidates.sort(key=lambda load: haversine_km(truck.platform.current_location, load.public.origin))
    return candidates


def propose_truck_candidates(load: Load, available_trucks: list[Truck], matching_cfg: MatchingConfig) -> list[Truck]:
    """Mirror of propose_candidates for the opposite direction -- interactive
    mode's shipper-visitor path (one load, screening candidate trucks) needs
    this where batch mode never did (it only ever decides truck-first, see
    simulator.py). Same visibility rule: only a truck's platform tier and a
    load's public tier are read, never either side's private data."""
    candidates = [
        truck
        for truck in available_trucks
        if load.public.weight_kg <= truck.platform.spare_capacity_weight_kg
        and load.public.volume_m3 <= truck.platform.spare_capacity_volume_m3
        and haversine_km(truck.platform.current_location, load.public.origin) <= matching_cfg.max_candidate_detour_km
    ]
    candidates.sort(key=lambda truck: haversine_km(truck.platform.current_location, load.public.origin))
    return candidates

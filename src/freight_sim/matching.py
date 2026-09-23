"""Two deliberately different levels of scrutiny, matching the trust model:

propose_candidates() is what the marketplace can do: it only ever sees public
Load terms and a truck's *platform* tier (location, status, spare capacity).
It has no access to a truck's private reservation price, max detour or home
deadline, so it can only screen coarsely (capacity fit + a generous straight-
line distance cap from config).

verify_match() is the deterministic optimiser: it has full information
(this is the "route optimiser verifies every deal and has veto power" design
decision) and checks real road distance/time against the truck's actual
private constraints. It is the only place a match can be vetoed, and it is
supposed to catch cases the coarse proposal missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from freight_sim.config import MatchingConfig
from freight_sim.events import VetoReason
from freight_sim.geo import haversine_km
from freight_sim.models import Load, Truck
from freight_sim.network import RoadNetwork


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


@dataclass
class VerifyResult:
    ok: bool
    reason: VetoReason | None
    detour_km: float
    detour_min: float
    pickup_eta: datetime
    delivery_eta: datetime


def verify_match(truck: Truck, load: Load, network: RoadNetwork, depart_time: datetime) -> VerifyResult:
    if load.public.weight_kg > truck.platform.spare_capacity_weight_kg or load.public.volume_m3 > truck.platform.spare_capacity_volume_m3:
        return VerifyResult(False, VetoReason.CAPACITY, 0.0, 0.0, depart_time, depart_time)

    detour_km = network.distance_km(truck.platform.current_location, load.public.origin)
    detour_min = network.travel_time_min(truck.platform.current_location, load.public.origin, depart_time)
    if detour_km > truck.private.max_detour_km:
        return VerifyResult(False, VetoReason.DETOUR_LIMIT, detour_km, detour_min, depart_time, depart_time)

    pickup_arrival = depart_time + timedelta(minutes=detour_min)
    pickup_window_start, pickup_window_end = load.public.pickup_window
    if pickup_arrival > pickup_window_end:
        return VerifyResult(False, VetoReason.TIME_WINDOW, detour_km, detour_min, pickup_arrival, pickup_arrival)
    pickup_time = max(pickup_arrival, pickup_window_start)

    loaded_departure = pickup_time + timedelta(minutes=load.public.loading_time_min)
    leg_min = network.travel_time_min(load.public.origin, load.public.destination, loaded_departure)
    delivery_arrival = loaded_departure + timedelta(minutes=leg_min)
    delivery_window_start, delivery_window_end = load.public.delivery_window
    if delivery_arrival > delivery_window_end:
        return VerifyResult(False, VetoReason.TIME_WINDOW, detour_km, detour_min, pickup_time, delivery_arrival)

    completion_time = delivery_arrival + timedelta(minutes=load.public.unloading_time_min)

    if truck.private.home_by is not None:
        home_leg_min = network.travel_time_min(load.public.destination, truck.private.home_location, completion_time)
        arrival_home = completion_time + timedelta(minutes=home_leg_min)
        if arrival_home > truck.private.home_by:
            return VerifyResult(False, VetoReason.HOME_DEADLINE, detour_km, detour_min, pickup_time, delivery_arrival)

    return VerifyResult(True, None, detour_km, detour_min, pickup_time, delivery_arrival)

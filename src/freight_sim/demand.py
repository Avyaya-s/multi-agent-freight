"""Fleet and load generation.

The directional imbalance that makes trucks run empty in the first place lives
here: each zone posts an outbound and an inbound load rate independently
(an industrial zone like Peenya ships far more than it receives), and a load's
destination zone is drawn weighted by the destination's own inbound rate, so
"consumption" zones like Whitefield pull in freight that has nowhere
symmetric to send back.
"""

from __future__ import annotations

import itertools
import math
import random
from datetime import timedelta

from freight_sim.clock import EPOCH
from freight_sim.config import DemandConfig, FleetConfig, Range, ZoneConfig
from freight_sim.models import (
    Load,
    LoadPrivate,
    LoadPublic,
    LoadStatus,
    Truck,
    TruckPlatform,
    TruckPrivate,
    TruckPublic,
    TruckStatus,
)
from freight_sim.network import RoadNetwork


def _uniform(rng: random.Random, r: Range) -> float:
    return rng.uniform(r.min, r.max)


def _uniform_int(rng: random.Random, r: Range) -> int:
    return rng.randint(int(r.min), int(r.max))


def _mid(r: Range) -> float:
    return (r.min + r.max) / 2.0


def _jitter(zone: ZoneConfig, rng: random.Random, radius_km: float = 1.5) -> tuple[float, float]:
    angle = rng.uniform(0, 2 * math.pi)
    dist_km = rng.uniform(0, radius_km)
    dlat = (dist_km * math.cos(angle)) / 111.0
    dlon = (dist_km * math.sin(angle)) / (111.0 * math.cos(math.radians(zone.lat)))
    return zone.lat + dlat, zone.lon + dlon


def generate_fleet(fleet_cfg: FleetConfig, zones: list[ZoneConfig], network: RoadNetwork, rng: random.Random) -> list[Truck]:
    trucks = []
    for i in range(fleet_cfg.num_trucks):
        zone = rng.choice(zones)
        lat, lon = _jitter(zone, rng)
        home = network.locate(lat, lon)

        capacity_weight = _uniform(rng, fleet_cfg.capacity_weight_kg)
        capacity_volume = _uniform(rng, fleet_cfg.capacity_volume_m3)

        home_by = None
        if rng.random() < fleet_cfg.home_deadline_probability:
            home_by = EPOCH + timedelta(days=rng.randint(3, 7), hours=rng.uniform(6, 20))

        truck_id = f"truck-{i:03d}"
        public = TruckPublic(
            truck_id=truck_id,
            owner_id=f"owner-{i:03d}",
            capacity_weight_kg=capacity_weight,
            capacity_volume_m3=capacity_volume,
        )
        platform = TruckPlatform(
            current_location=home,
            status=TruckStatus.IDLE,
            available_from=EPOCH,
            spare_capacity_weight_kg=capacity_weight,
            spare_capacity_volume_m3=capacity_volume,
        )
        private = TruckPrivate(
            fixed_cost_per_day=_uniform(rng, fleet_cfg.fixed_cost_per_day),
            variable_cost_per_km=_uniform(rng, fleet_cfg.variable_cost_per_km),
            fuel_efficiency_kmpl=_uniform(rng, fleet_cfg.fuel_efficiency_kmpl),
            variable_cost_per_hour_waiting=_uniform(rng, fleet_cfg.variable_cost_per_hour_waiting),
            reservation_rate_per_km=_uniform(rng, fleet_cfg.reservation_rate_per_km),
            return_leg_discount=_uniform(rng, fleet_cfg.return_leg_discount),
            home_location=home,
            home_by=home_by,
            max_detour_km=_uniform(rng, fleet_cfg.max_detour_km),
            max_wait_hours=_uniform(rng, fleet_cfg.max_wait_hours),
        )
        trucks.append(Truck(public=public, platform=platform, private=private))
    return trucks


def _pick_destination_zone(zones: list[ZoneConfig], origin_zone: ZoneConfig, rng: random.Random) -> ZoneConfig:
    candidates = [z for z in zones if z.name != origin_zone.name]
    weights = [_mid(z.inbound_loads_per_day) for z in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]


def generate_loads_for_day(
    demand_cfg: DemandConfig,
    network: RoadNetwork,
    rng: random.Random,
    day_index: int,
) -> list[Load]:
    loads = []
    day_start = EPOCH + timedelta(days=day_index)
    counter = itertools.count()

    for zone in demand_cfg.zones:
        n_outbound = _uniform_int(rng, zone.outbound_loads_per_day)
        for _ in range(n_outbound):
            dest_zone = _pick_destination_zone(demand_cfg.zones, zone, rng)
            olat, olon = _jitter(zone, rng)
            dlat, dlon = _jitter(dest_zone, rng)
            origin = network.locate(olat, olon)
            destination = network.locate(dlat, dlon)

            pickup_start = day_start + timedelta(hours=rng.uniform(6, 18))
            pickup_end = pickup_start + timedelta(hours=demand_cfg.pickup_window_hours)

            loading_min = _uniform(rng, demand_cfg.loading_time_min)
            unloading_min = _uniform(rng, demand_cfg.unloading_time_min)
            travel_min = network.travel_time_min(origin, destination, pickup_start)
            slack = _uniform(rng, demand_cfg.delivery_slack_hours)
            delivery_start = pickup_start + timedelta(minutes=loading_min + travel_min)
            delivery_end = delivery_start + timedelta(hours=slack)

            posted_rate = _uniform(rng, demand_cfg.posted_rate_per_km)
            markup = _uniform(rng, demand_cfg.reservation_rate_per_km_markup)

            load_id = f"load-{day_index:02d}-{zone.name[:3].lower()}-{next(counter):03d}"
            public = LoadPublic(
                load_id=load_id,
                shipper_id=f"shipper-{zone.name.lower()}",
                origin=origin,
                destination=destination,
                weight_kg=_uniform(rng, demand_cfg.weight_kg),
                volume_m3=_uniform(rng, demand_cfg.volume_m3),
                pickup_window=(pickup_start, pickup_end),
                delivery_window=(delivery_start, delivery_end),
                posted_rate_per_km=posted_rate,
                loading_time_min=loading_min,
                unloading_time_min=unloading_min,
                posted_at=day_start,
                status=LoadStatus.OPEN,
            )
            private = LoadPrivate(
                reservation_rate_per_km=posted_rate * (1 + markup),
                penalty_per_hour_late=_uniform(rng, demand_cfg.penalty_per_hour_late),
            )
            loads.append(Load(public=public, private=private))
    return loads

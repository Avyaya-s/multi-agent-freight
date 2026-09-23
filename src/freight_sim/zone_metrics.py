"""Per-zone / per-truck breakdowns that reveal directional-imbalance effects
the aggregate Metrics in metrics.py can wash out.

Why this exists: an aggregate empty-km reduction that stays flat across an
imbalance sweep doesn't mean imbalance has no effect -- it means the effect
isn't visible in a single mean. Imbalance should show up as a *gap between
zones* (net-exporter zones vs net-importer zones), not as a shift in the
overall average. This module measures that gap directly. See README's
"distributional sanity check" section.

Still reads the event log only, never live simulator state; still an
omniscient/diagnostic tool, not something an agent would ever see.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from freight_sim.config import ZoneConfig
from freight_sim.events import EventType, read_events
from freight_sim.geo import haversine_km
from freight_sim.models import Location


def _nearest_zone(lat: float, lon: float, zones: list[ZoneConfig]) -> str:
    loc = Location(lat=lat, lon=lon)
    return min(zones, key=lambda z: haversine_km(loc, Location(lat=z.lat, lon=z.lon))).name


@dataclass
class ZoneMetrics:
    # Of every time a truck was away from home in this zone and had to decide
    # what's next, the fraction that found a loaded backhaul there. None if
    # the zone had no such decisions at all in the run(s) analyzed.
    backhaul_success_rate_by_zone: dict[str, float | None] = field(default_factory=dict)
    # Of loads posted with this zone as their origin, the fraction that expired unmatched.
    load_expiry_rate_by_zone: dict[str, float | None] = field(default_factory=dict)
    earnings_by_home_zone: dict[str, list[float]] = field(default_factory=dict)
    earnings_mean_by_home_zone: dict[str, float] = field(default_factory=dict)
    earnings_stdev_by_home_zone: dict[str, float] = field(default_factory=dict)
    overall_earnings_mean: float = 0.0
    overall_earnings_stdev: float = 0.0


def compute_zone_metrics(log_paths: list[str | Path], zones: list[ZoneConfig]) -> ZoneMetrics:
    """Aggregates raw counts across all given logs before computing any rate,
    so combining several seeds doesn't average small-sample noise -- see
    scripts/run_zone_imbalance_sweep.py, which passes one log per seed."""
    zone_names = [z.name for z in zones]

    home_zone_by_truck: dict[str, str] = {}
    origin_zone_by_load: dict[str, str] = {}
    posted_by_zone: Counter = Counter()
    expired_by_zone: Counter = Counter()
    backhaul_by_zone: Counter = Counter()
    deadhead_by_zone: Counter = Counter()
    earnings_by_truck: dict[str, float] = {}

    for log_path in log_paths:
        for e in read_events(Path(log_path)):
            if e.event_type == EventType.TRUCK_SPAWNED:
                home = e.payload["home_location"]
                home_zone_by_truck[e.entity_id] = _nearest_zone(home["lat"], home["lon"], zones)
                earnings_by_truck.setdefault(e.entity_id, 0.0)

            elif e.event_type == EventType.LOAD_POSTED:
                origin = e.payload["origin"]
                zone = _nearest_zone(origin["lat"], origin["lon"], zones)
                origin_zone_by_load[e.entity_id] = zone
                posted_by_zone[zone] += 1

            elif e.event_type == EventType.LOAD_EXPIRED:
                zone = origin_zone_by_load.get(e.entity_id)
                if zone:
                    expired_by_zone[zone] += 1

            elif e.event_type == EventType.TRUCK_DEPARTED:
                purpose = e.payload["purpose"]
                frm = e.payload["from"]
                zone = _nearest_zone(frm["lat"], frm["lon"], zones)
                if purpose == "reposition_to_pickup" and e.payload.get("is_backhaul"):
                    backhaul_by_zone[zone] += 1
                elif purpose == "deadhead_home":
                    deadhead_by_zone[zone] += 1

            elif e.event_type == EventType.DEAL_COMPLETED:
                truck_id = e.payload["truck_id"]
                net = e.payload["revenue"] - e.payload["commission_paid"]
                earnings_by_truck[truck_id] = earnings_by_truck.get(truck_id, 0.0) + net

    backhaul_success_rate_by_zone = {}
    for zone in zone_names:
        denom = backhaul_by_zone[zone] + deadhead_by_zone[zone]
        backhaul_success_rate_by_zone[zone] = (backhaul_by_zone[zone] / denom) if denom else None

    load_expiry_rate_by_zone = {}
    for zone in zone_names:
        posted = posted_by_zone[zone]
        load_expiry_rate_by_zone[zone] = (expired_by_zone[zone] / posted) if posted else None

    earnings_by_home_zone: dict[str, list[float]] = defaultdict(list)
    for truck_id, home_zone in home_zone_by_truck.items():
        earnings_by_home_zone[home_zone].append(earnings_by_truck.get(truck_id, 0.0))

    earnings_mean_by_home_zone = {z: (statistics.mean(v) if v else 0.0) for z, v in earnings_by_home_zone.items()}
    earnings_stdev_by_home_zone = {z: (statistics.stdev(v) if len(v) > 1 else 0.0) for z, v in earnings_by_home_zone.items()}

    all_earnings = list(earnings_by_truck.values())

    return ZoneMetrics(
        backhaul_success_rate_by_zone=backhaul_success_rate_by_zone,
        load_expiry_rate_by_zone=load_expiry_rate_by_zone,
        earnings_by_home_zone=dict(earnings_by_home_zone),
        earnings_mean_by_home_zone=earnings_mean_by_home_zone,
        earnings_stdev_by_home_zone=earnings_stdev_by_home_zone,
        overall_earnings_mean=statistics.mean(all_earnings) if all_earnings else 0.0,
        overall_earnings_stdev=statistics.stdev(all_earnings) if len(all_earnings) > 1 else 0.0,
    )

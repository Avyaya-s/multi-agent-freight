"""Straight-line geography helper.

Used only for the marketplace's coarse candidate screen (matching.propose_candidates),
which by design only ever sees public/platform fields -- it has no access to real
road distance reasoning tied to a truck's private constraints. Real road distance
comes from network.RoadNetwork and is what the optimiser (matching.verify_match)
uses to actually verify or veto a match.
"""

from __future__ import annotations

import math

from freight_sim.models import Location

EARTH_RADIUS_KM = 6371.0


def haversine_km(a: Location, b: Location) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))

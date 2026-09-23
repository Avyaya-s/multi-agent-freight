"""Real road network for one Indian industrial region, via OSMnx.

Distance and free-flow travel time come from actual OpenStreetMap road
geometry, not straight-line distance. Travel time is then scaled by the
hour-of-day multiplier in config (network.travel_time_hourly_multipliers) to
model rush-hour slowdowns.

The graph is downloaded once and cached to disk (network.cache_path); every
later run loads the cached copy so results are reproducible without depending
on OSM being reachable.

Performance note: a single point-to-point Dijkstra query on this graph
(~240k nodes / 600k edges) costs roughly 2 seconds in networkx, and a
simulation makes thousands of such queries. Two caches make this tractable
without sacrificing the "real road network" premise:

  - locate() snaps lat/lon to a coarse grid before resolving the nearest OSM
    node, so the many jittered pickup/drop points generated within ~1.5km of
    the same zone (see demand.py) resolve to a shared handful of nodes rather
    than each needing its own path search. This trades a small amount of
    positional precision for a large drop in distinct routes -- an explicit,
    acceptable trade given the project's own standard is structural realism,
    not numeric accuracy.
  - route metrics (distance + free-flow time) are computed once per node pair
    and cached; distance_km/travel_time_min for the same pair never re-run
    Dijkstra.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import networkx as nx
import osmnx as ox

from freight_sim.config import NetworkConfig
from freight_sim.models import Location

# ~2.2km. Jitter radius in demand.py is 1.5km, so a zone's jittered points
# span only a few of these cells, which is what makes the route cache effective.
GRID_DEGREES = 0.02


class RoadNetwork:
    def __init__(self, graph: nx.MultiDiGraph, hourly_multipliers: dict[int, float]):
        self.graph = graph
        self.hourly_multipliers = hourly_multipliers
        self._node_cache: dict[tuple[float, float], int] = {}
        self._route_cache: dict[tuple[int, int], tuple[float, float]] = {}  # (node_a, node_b) -> (meters, free_flow_seconds)

    def clear_cache(self) -> None:
        """Resets the in-memory node/route caches. Needed for a fair cold-cache
        timing comparison across configs that change what gets queried (e.g.
        the coarse-filter sweep) -- without this, whichever config runs first
        would unfairly warm the cache for the ones after it."""
        self._node_cache.clear()
        self._route_cache.clear()

    @classmethod
    def load(cls, config: NetworkConfig) -> "RoadNetwork":
        cache_path = Path(config.cache_path)
        if cache_path.exists():
            # osmnx's default dtype converters restore length/speed_kph/travel_time
            # (and node x/y) to float on load, so no manual conversion is needed.
            graph = ox.load_graphml(cache_path)
        else:
            bbox = (config.bbox.west, config.bbox.south, config.bbox.east, config.bbox.north)
            graph = ox.graph_from_bbox(bbox, network_type=config.network_type)
            graph = ox.add_edge_speeds(graph)
            graph = ox.add_edge_travel_times(graph)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            ox.save_graphml(graph, cache_path)
        return cls(graph, config.travel_time_hourly_multipliers)

    def locate(self, lat: float, lon: float) -> Location:
        grid_key = (round(lat / GRID_DEGREES) * GRID_DEGREES, round(lon / GRID_DEGREES) * GRID_DEGREES)
        node_id = self._node_cache.get(grid_key)
        if node_id is None:
            node_id = ox.nearest_nodes(self.graph, X=grid_key[1], Y=grid_key[0])
            self._node_cache[grid_key] = node_id
        return Location(lat=lat, lon=lon, node_id=node_id)

    def distance_km(self, origin: Location, destination: Location) -> float:
        meters, _ = self._route_metrics(origin, destination)
        return meters / 1000.0

    def travel_time_min(self, origin: Location, destination: Location, depart_time: datetime) -> float:
        _, free_flow_seconds = self._route_metrics(origin, destination)
        multiplier = self.hourly_multipliers[depart_time.hour]
        return (free_flow_seconds * multiplier) / 60.0

    def _route_metrics(self, origin: Location, destination: Location) -> tuple[float, float]:
        if origin.node_id is None or destination.node_id is None:
            raise ValueError("Location must be resolved via RoadNetwork.locate() first")
        if origin.node_id == destination.node_id:
            return 0.0, 0.0

        key = (origin.node_id, destination.node_id)
        cached = self._route_cache.get(key)
        if cached is not None:
            return cached

        route = ox.shortest_path(self.graph, origin.node_id, destination.node_id, weight="travel_time")
        if route is None:
            raise ValueError(f"No route between {origin} and {destination}")

        meters = 0.0
        free_flow_seconds = 0.0
        for u, v in zip(route[:-1], route[1:]):
            edge = self.graph.edges[u, v, 0]
            meters += edge["length"]
            free_flow_seconds += edge["travel_time"]

        self._route_cache[key] = (meters, free_flow_seconds)
        return meters, free_flow_seconds

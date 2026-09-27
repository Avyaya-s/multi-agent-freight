"""Stage 1's two-layer feasibility check, split so the platform can never be
made to read a truck's private data:

  PHYSICAL layer (check_feasibility) -- road network, capacity (weight and
  volume), pickup/delivery time windows, service (loading/unloading) times.
  Public/platform data only. This is what OR-Tools solves. Deferred: driver
  hours-of-service -- Stage 0/1 has no shift-length or rest-rule data model
  to check it against, so it is flagged here rather than faked.

  PREFERENCE layer (check_preference) -- the truck owner's own standing
  thresholds: max_detour_km, home_by, reservation_rate_per_km. Uses
  TruckPrivate. In Stage 1 this is a deterministic filter standing in for
  the trucker agent; Stage 2 puts an LLM agent at this exact boundary
  instead, so the PHYSICAL layer never has to change.

Every result carries `layer` and (on failure) `reason`, so a veto is always
attributable to "physically impossible" or "owner wouldn't accept" -- these
are different findings and must not be pooled into one veto rate (see
metrics.veto_rate_by_layer).

Determinism: the OR-Tools search uses a fixed time limit and first-solution
strategy with no local-search metaheuristic enabled, so identical input
always produces identical output (see tests/test_feasibility.py).

Timing: each call measures its own solver_ms via time.perf_counter() around
just the solve, not wall-clock time. See README's coarse-filter sweep for why
wall-clock time is the wrong instrument at this scale -- solver_ms is not
affected by unrelated demand-generation cost the way a wall timer would be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Sequence

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from freight_sim.config import SolverConfig
from freight_sim.events import FeasibilityLayer, VetoReason
from freight_sim.models import Load, Location, TruckPlatform, TruckPrivate
from freight_sim.network import RoadNetwork

# Minutes; generous enough to cover same-day and next-day windows without
# the integer time dimension overflowing.
_HORIZON_MIN = 24 * 60 * 3


@dataclass
class FeasibilityResult:
    feasible: bool
    layer: FeasibilityLayer
    reason: VetoReason | None = None
    detour_km: float | None = None
    detour_min: float | None = None
    pickup_eta: datetime | None = None
    delivery_eta: datetime | None = None
    slack_min: float | None = None
    marginal_cost: float | None = None
    solver_ms: float = 0.0


def check_feasibility(
    truck_platform: TruckPlatform,
    candidate_loads: Sequence[Load],
    committed_legs: Sequence[object],
    network: RoadNetwork,
    depart_time: datetime,
    solver_config: SolverConfig,
) -> list[FeasibilityResult]:
    """The PHYSICAL layer. Takes a batch so the interface doesn't need to
    change when a later stage (Stage 5's auction) needs to evaluate bundles;
    Stage 1 itself still checks each candidate independently, not jointly.

    committed_legs is reserved for a truck that already has other stops on
    its route -- always empty in Stage 0/1, where a truck only ever commits
    to one job at a time. Non-empty is not yet supported.
    """
    if committed_legs:
        raise NotImplementedError(
            "committed_legs is reserved for a later stage; Stage 1 only evaluates "
            "a truck with nothing already committed."
        )
    return [_check_one_physical(truck_platform, load, network, depart_time, solver_config) for load in candidate_loads]


def _check_one_physical(
    truck_platform: TruckPlatform,
    load: Load,
    network: RoadNetwork,
    depart_time: datetime,
    solver_config: SolverConfig,
) -> FeasibilityResult:
    # Fast pre-check: no point invoking a solver for something one comparison
    # already answers, and it keeps a guaranteed-infeasible candidate cheap.
    if load.public.weight_kg > truck_platform.spare_capacity_weight_kg or load.public.volume_m3 > truck_platform.spare_capacity_volume_m3:
        return FeasibilityResult(feasible=False, layer=FeasibilityLayer.PHYSICAL, reason=VetoReason.CAPACITY, solver_ms=0.0)

    t0 = time.perf_counter()

    origin, destination = load.public.origin, load.public.destination
    locations: list[Location] = [truck_platform.current_location, origin, destination]

    # Static transit matrix evaluated at depart_time -- a documented
    # simplification. A single delivery spans a few hours at most, so treating
    # travel speed as constant across it is a reasonable approximation; this
    # is not a time-dependent VRP.
    travel_min = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            if i != j:
                travel_min[i][j] = network.travel_time_min(locations[i], locations[j], depart_time)
    service_min = [0.0, load.public.loading_time_min, load.public.unloading_time_min]

    # Open route: starts at node 0 (current location), ends at node 2
    # (delivery) -- NOT a round trip back to node 0. A single shared depot
    # index would force an implicit return leg the truck never actually
    # drives, corrupting both the cost and the time-window feasibility check.
    manager = pywrapcp.RoutingIndexManager(3, 1, [0], [2])
    routing = pywrapcp.RoutingModel(manager)

    def transit_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(round(travel_min[from_node][to_node] + service_min[from_node]))

    transit_idx = routing.RegisterTransitCallback(transit_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # slack_max = horizon: a vehicle may wait at a node for a window to open.
    routing.AddDimension(transit_idx, _HORIZON_MIN, _HORIZON_MIN, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    def to_minutes(dt: datetime) -> int:
        return int(round((dt - depart_time).total_seconds() / 60))

    start_index = routing.Start(0)
    time_dim.CumulVar(start_index).SetRange(0, 0)  # departs now, not earlier or later

    # Node 2 is this vehicle's designated *end* node (see the open-route
    # comment above): manager.NodeToIndex(2) returns -1 for it (that's the
    # documented behavior for a start/end node), which silently produces an
    # invalid variable and crashes the native solver on SetRange rather than
    # raising a clean Python exception. End nodes must be addressed via
    # routing.End(vehicle), not NodeToIndex.
    pickup_index = manager.NodeToIndex(1)
    delivery_index = routing.End(0)
    for index, (window_start, window_end) in (
        (pickup_index, load.public.pickup_window),
        (delivery_index, load.public.delivery_window),
    ):
        lo = max(0, to_minutes(window_start))
        hi = max(lo, to_minutes(window_end))
        time_dim.CumulVar(index).SetRange(lo, hi)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = getattr(
        routing_enums_pb2.FirstSolutionStrategy, solver_config.first_solution_strategy
    )
    search_parameters.time_limit.FromMilliseconds(int(solver_config.time_limit_ms))

    solution = routing.SolveWithParameters(search_parameters)
    solver_ms = (time.perf_counter() - t0) * 1000

    if solution is None:
        return FeasibilityResult(feasible=False, layer=FeasibilityLayer.PHYSICAL, reason=VetoReason.TIME_WINDOW, solver_ms=solver_ms)

    pickup_min = solution.Value(time_dim.CumulVar(pickup_index))
    delivery_min = solution.Value(time_dim.CumulVar(delivery_index))
    pickup_eta = depart_time + timedelta(minutes=pickup_min)
    delivery_eta = depart_time + timedelta(minutes=delivery_min)

    pickup_slack = to_minutes(load.public.pickup_window[1]) - pickup_min
    delivery_slack = to_minutes(load.public.delivery_window[1]) - delivery_min

    return FeasibilityResult(
        feasible=True,
        layer=FeasibilityLayer.PHYSICAL,
        reason=None,
        detour_km=network.distance_km(truck_platform.current_location, origin),
        detour_min=travel_min[0][1],
        pickup_eta=pickup_eta,
        delivery_eta=delivery_eta,
        slack_min=min(pickup_slack, delivery_slack),
        marginal_cost=None,
        solver_ms=solver_ms,
    )


def check_preference(
    truck_private: TruckPrivate,
    load: Load,
    physical: FeasibilityResult,
    is_backhaul: bool,
    network: RoadNetwork,
    check_rate: bool = True,
) -> FeasibilityResult:
    """The PREFERENCE layer -- Stage 1's deterministic stand-in for the
    trucker agent. Only ever called on a candidate the PHYSICAL layer already
    found feasible; reuses its detour/ETA figures rather than re-querying the
    network for anything the physical layer already computed.

    check_rate=False is an ablation hook only (scripts/run_rate_ablation.py):
    it reproduces Stage 0's exact constraint set (detour + home deadline, no
    rate) on this architecture, isolating "is OR-Tools stricter than
    arithmetic?" from "does the rate check (new in Stage 1) change the
    result?" Always True in ordinary use.
    """
    t0 = time.perf_counter()

    def veto(reason: VetoReason) -> FeasibilityResult:
        return replace(physical, feasible=False, layer=FeasibilityLayer.PREFERENCE, reason=reason, marginal_cost=None, solver_ms=(time.perf_counter() - t0) * 1000)

    if physical.detour_km > truck_private.max_detour_km:
        return veto(VetoReason.DETOUR_LIMIT)

    if check_rate:
        threshold = truck_private.reservation_rate_per_km * (1 - truck_private.return_leg_discount) if is_backhaul else truck_private.reservation_rate_per_km
        if load.public.posted_rate_per_km < threshold:
            return veto(VetoReason.RATE_TOO_LOW)

    if truck_private.home_by is not None:
        completion_time = physical.delivery_eta + timedelta(minutes=load.public.unloading_time_min)
        home_leg_min = network.travel_time_min(load.public.destination, truck_private.home_location, completion_time)
        if completion_time + timedelta(minutes=home_leg_min) > truck_private.home_by:
            return veto(VetoReason.HOME_DEADLINE)

    leg_km = network.distance_km(load.public.origin, load.public.destination)
    marginal_cost = (physical.detour_km + leg_km) * truck_private.variable_cost_per_km

    return replace(
        physical,
        feasible=True,
        layer=FeasibilityLayer.PREFERENCE,
        reason=None,
        marginal_cost=marginal_cost,
        solver_ms=(time.perf_counter() - t0) * 1000,
    )

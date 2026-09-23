"""The Stage 0 discrete-event simulator.

One event-queue entry type per *decision point*, not per physical event: once
a match is verified, the whole deal (reposition -> load -> deliver) is written
to the log in one synchronous pass with correctly time-stamped events, and
only the truck's *next* decision point (its next `truck_available`) goes back
on the heap. Nothing in Stage 0 needs to interrupt a truck mid-leg (no
breakdowns, no re-negotiation), so this keeps the scheduler small without
losing any timestamp accuracy in the log.

Waiting/idle re-checks use a per-truck token: starting a new episode (or
ending one, by matching or giving up) bumps the token, so a stale retry or
give-up timeout scheduled from an episode that has already ended is a silent
no-op instead of double-logging.
"""

from __future__ import annotations

import heapq
import itertools
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from freight_sim.clock import EPOCH
from freight_sim.config import Config
from freight_sim.demand import generate_fleet, generate_loads_for_day
from freight_sim.events import (
    DeadheadReason,
    DeparturePurpose,
    EventLog,
    EventType,
)
from freight_sim.geo import haversine_km
from freight_sim.matching import propose_candidates, verify_match
from freight_sim.models import (
    Deal,
    DealStatus,
    Load,
    LoadStatus,
    Location,
    MatchMechanism,
    Truck,
    TruckStatus,
)
from freight_sim.network import RoadNetwork

HOME_PROXIMITY_KM = 0.5


def _loc(location: Location) -> dict[str, float]:
    return {"lat": location.lat, "lon": location.lon}


class Simulator:
    def __init__(self, config: Config, network: RoadNetwork, mechanism: MatchMechanism, run_id: str, log_path: str | Path):
        self.config = config
        self.network = network
        self.mechanism = mechanism
        self.rng = random.Random(config.run.seed)
        self.log = EventLog(Path(log_path), run_id)

        self._heap: list[tuple[datetime, int, str, dict[str, Any]]] = []
        self._seq = itertools.count()
        self.trucks: dict[str, Truck] = {}
        self.loads: dict[str, Load] = {}
        self.deals: dict[str, Deal] = {}
        self._token: dict[str, int] = {}
        self._episode_start: dict[str, datetime] = {}

        self.end_time = EPOCH + timedelta(days=config.run.sim_days) + timedelta(hours=48)

    # ------------------------------------------------------------------ #
    # setup / run loop
    # ------------------------------------------------------------------ #

    def _schedule(self, time: datetime, kind: str, data: dict[str, Any]) -> None:
        heapq.heappush(self._heap, (time, next(self._seq), kind, data))

    def run(self) -> None:
        self.log.emit(
            EPOCH,
            EventType.SIM_STARTED,
            "sim",
            {
                "seed": self.config.run.seed,
                "mechanism": self.mechanism.value,
                "config_snapshot": self.config.model_dump(mode="json"),
            },
        )

        for day in range(self.config.run.sim_days):
            self._schedule(EPOCH + timedelta(days=day), "post_loads", {"day_index": day})

        fleet = generate_fleet(self.config.fleet, self.config.demand.zones, self.network, self.rng)
        self.trucks = {t.truck_id: t for t in fleet}
        for truck in fleet:
            self._emit_truck_spawned(truck)
            self._schedule(EPOCH, "truck_available", {"truck_id": truck.truck_id})

        handlers = {
            "post_loads": self._handle_post_loads,
            "load_expire_check": self._handle_load_expire_check,
            "truck_available": self._handle_truck_available,
            "truck_retry": self._handle_truck_retry,
            "truck_wait_timeout": self._handle_truck_wait_timeout,
        }

        while self._heap:
            time, _seq, kind, data = heapq.heappop(self._heap)
            if time > self.end_time:
                continue
            handlers[kind](time, data)

        self.log.emit(self.end_time, EventType.SIM_ENDED, "sim", {})
        self.log.close()

    def _bump_token(self, truck_id: str) -> int:
        self._token[truck_id] = self._token.get(truck_id, 0) + 1
        return self._token[truck_id]

    # ------------------------------------------------------------------ #
    # load board
    # ------------------------------------------------------------------ #

    def _handle_post_loads(self, time: datetime, data: dict[str, Any]) -> None:
        new_loads = generate_loads_for_day(self.config.demand, self.network, self.rng, data["day_index"])
        for load in new_loads:
            self.loads[load.load_id] = load
            p = load.public
            self.log.emit(
                time,
                EventType.LOAD_POSTED,
                load.load_id,
                {
                    "shipper_id": p.shipper_id,
                    "origin": _loc(p.origin),
                    "destination": _loc(p.destination),
                    "weight_kg": p.weight_kg,
                    "volume_m3": p.volume_m3,
                    "pickup_window": [p.pickup_window[0].isoformat(), p.pickup_window[1].isoformat()],
                    "delivery_window": [p.delivery_window[0].isoformat(), p.delivery_window[1].isoformat()],
                    "posted_rate_per_km": p.posted_rate_per_km,
                    "loading_time_min": p.loading_time_min,
                    "unloading_time_min": p.unloading_time_min,
                    "reservation_rate_per_km": load.private.reservation_rate_per_km,
                    "penalty_per_hour_late": load.private.penalty_per_hour_late,
                },
            )
            self._schedule(p.pickup_window[1], "load_expire_check", {"load_id": load.load_id})

    def _handle_load_expire_check(self, time: datetime, data: dict[str, Any]) -> None:
        load = self.loads.get(data["load_id"])
        if load and load.public.status == LoadStatus.OPEN:
            load.public.status = LoadStatus.EXPIRED
            self.log.emit(time, EventType.LOAD_EXPIRED, load.load_id, {"reason": "no_match_before_pickup_window_close"})

    # ------------------------------------------------------------------ #
    # truck decision points
    # ------------------------------------------------------------------ #

    def _emit_truck_spawned(self, truck: Truck) -> None:
        pub, plat, priv = truck.public, truck.platform, truck.private
        self.log.emit(
            EPOCH,
            EventType.TRUCK_SPAWNED,
            truck.truck_id,
            {
                "owner_id": pub.owner_id,
                "capacity_weight_kg": pub.capacity_weight_kg,
                "capacity_volume_m3": pub.capacity_volume_m3,
                "spawn_location": _loc(plat.current_location),
                "home_location": _loc(priv.home_location),
                "home_by": priv.home_by.isoformat() if priv.home_by else None,
                "fixed_cost_per_day": priv.fixed_cost_per_day,
                "variable_cost_per_km": priv.variable_cost_per_km,
                "fuel_efficiency_kmpl": priv.fuel_efficiency_kmpl,
                "variable_cost_per_hour_waiting": priv.variable_cost_per_hour_waiting,
                "reservation_rate_per_km": priv.reservation_rate_per_km,
                "return_leg_discount": priv.return_leg_discount,
                "max_detour_km": priv.max_detour_km,
                "max_wait_hours": priv.max_wait_hours,
            },
        )

    def _handle_truck_available(self, time: datetime, data: dict[str, Any]) -> None:
        truck = self.trucks[data["truck_id"]]
        self._decide_fresh(truck, time)

    def _decide_fresh(self, truck: Truck, time: datetime) -> None:
        """A truck has nothing committed right now.

        The baseline (mechanism == NONE) still dispatches a truck's next job the
        same way as the other mechanisms when it is at home -- that represents
        ordinary, non-marketplace freight matching (phone calls, regular
        customers), which does not stop existing just because this project's
        marketplace doesn't. What the baseline actually removes is backhaul
        chaining: a truck that just finished a delivery away from home has no
        marketplace to find a *return* load, so it always deadheads immediately
        instead of searching. That is the one deliberate behavioral difference
        between scenarios, isolating exactly the empty-return problem.
        """
        at_home = haversine_km(truck.platform.current_location, truck.private.home_location) <= HOME_PROXIMITY_KM
        if self.mechanism == MatchMechanism.NONE and not at_home:
            self._go_deadhead(truck, time, DeadheadReason.NO_LOADS_POSTED)
            return
        if self._past_home_deadline_buffer(truck, time):
            self._go_deadhead(truck, time, DeadheadReason.HOME_DEADLINE)
            return
        if self._attempt_match(truck, time):
            return
        self._start_episode(truck, time)

    def _past_home_deadline_buffer(self, truck: Truck, time: datetime) -> bool:
        home_by = truck.private.home_by
        if home_by is None:
            return False
        if haversine_km(truck.platform.current_location, truck.private.home_location) <= HOME_PROXIMITY_KM:
            return False
        home_leg_min = self.network.travel_time_min(truck.platform.current_location, truck.private.home_location, time)
        buffer = timedelta(hours=self.config.matching.home_deadline_buffer_hours)
        return time + timedelta(minutes=home_leg_min) + buffer >= home_by

    def _start_episode(self, truck: Truck, time: datetime) -> None:
        at_home = haversine_km(truck.platform.current_location, truck.private.home_location) <= HOME_PROXIMITY_KM
        token = self._bump_token(truck.truck_id)
        self._episode_start[truck.truck_id] = time
        if at_home:
            truck.platform.status = TruckStatus.IDLE
            self.log.emit(time, EventType.TRUCK_IDLE_STARTED, truck.truck_id, {"location": _loc(truck.platform.current_location)})
        else:
            truck.platform.status = TruckStatus.WAITING
            self.log.emit(time, EventType.TRUCK_WAIT_STARTED, truck.truck_id, {"location": _loc(truck.platform.current_location)})
            self._schedule(
                time + timedelta(hours=truck.private.max_wait_hours),
                "truck_wait_timeout",
                {"truck_id": truck.truck_id, "token": token},
            )
        self._schedule(
            time + timedelta(hours=self.config.matching.retry_interval_hours),
            "truck_retry",
            {"truck_id": truck.truck_id, "token": token},
        )

    def _end_episode(self, truck: Truck, time: datetime, was_waiting: bool) -> None:
        self._bump_token(truck.truck_id)  # invalidate any other pending retry/timeout for this episode
        episode_start = self._episode_start[truck.truck_id]
        elapsed_min = (time - episode_start).total_seconds() / 60.0
        if was_waiting:
            self.log.emit(time, EventType.TRUCK_WAIT_ENDED, truck.truck_id, {"waited_min": elapsed_min})
        else:
            self.log.emit(time, EventType.TRUCK_IDLE_ENDED, truck.truck_id, {"idle_min": elapsed_min})

    def _handle_truck_retry(self, time: datetime, data: dict[str, Any]) -> None:
        truck_id, token = data["truck_id"], data["token"]
        if self._token.get(truck_id) != token:
            return  # episode already ended from under this callback
        truck = self.trucks[truck_id]
        was_waiting = truck.platform.status == TruckStatus.WAITING

        # A NONE-mechanism truck can only ever reach a retry while idling at
        # home (see _decide_fresh), so _past_home_deadline_buffer is trivially
        # False for it and _attempt_match runs exactly like the other
        # mechanisms -- consistent with "ordinary dispatch still works."
        if self._past_home_deadline_buffer(truck, time):
            self._end_episode(truck, time, was_waiting)
            self._go_deadhead(truck, time, DeadheadReason.HOME_DEADLINE)
            return
        if self._attempt_match(truck, time):
            self._end_episode(truck, time, was_waiting)
            return
        # still nothing: keep waiting/idling, same episode
        self._schedule(
            time + timedelta(hours=self.config.matching.retry_interval_hours),
            "truck_retry",
            {"truck_id": truck_id, "token": token},
        )

    def _handle_truck_wait_timeout(self, time: datetime, data: dict[str, Any]) -> None:
        truck_id, token = data["truck_id"], data["token"]
        if self._token.get(truck_id) != token:
            return
        truck = self.trucks[truck_id]
        self._end_episode(truck, time, was_waiting=True)
        self._go_deadhead(truck, time, DeadheadReason.GAVE_UP)

    def _go_deadhead(self, truck: Truck, time: datetime, reason: DeadheadReason) -> None:
        if haversine_km(truck.platform.current_location, truck.private.home_location) <= HOME_PROXIMITY_KM:
            self._start_episode(truck, time)  # already home: this is an idle episode, not a trip
            return
        home = truck.private.home_location
        distance_km = self.network.distance_km(truck.platform.current_location, home)
        duration_min = self.network.travel_time_min(truck.platform.current_location, home, time)
        self.log.emit(
            time,
            EventType.TRUCK_DEPARTED,
            truck.truck_id,
            {
                "purpose": DeparturePurpose.DEADHEAD_HOME.value,
                "deadhead_reason": reason.value,
                "load_id": None,
                "from": _loc(truck.platform.current_location),
                "to": _loc(home),
                "distance_km": distance_km,
                "expected_duration_min": duration_min,
                "laden": False,
            },
        )
        truck.platform.status = TruckStatus.EN_ROUTE_EMPTY
        arrival = time + timedelta(minutes=duration_min)
        self.log.emit(
            arrival,
            EventType.TRUCK_ARRIVED,
            truck.truck_id,
            {"location": _loc(home), "distance_km": distance_km, "duration_min": duration_min},
        )
        truck.platform.current_location = home
        truck.platform.available_from = arrival
        self._schedule(arrival, "truck_available", {"truck_id": truck.truck_id})

    # ------------------------------------------------------------------ #
    # matching
    # ------------------------------------------------------------------ #

    def _attempt_match(self, truck: Truck, time: datetime) -> Load | None:
        # A match found while the truck is away from home is a real backhaul
        # (the empty-return problem); one found while at home is ordinary
        # dispatch that would happen with or without a marketplace. Metrics
        # (loaded_return_share) key off this distinction, not off "any deal."
        is_backhaul = haversine_km(truck.platform.current_location, truck.private.home_location) > HOME_PROXIMITY_KM

        open_loads = [load for load in self.loads.values() if load.public.status == LoadStatus.OPEN]
        candidates = propose_candidates(truck, open_loads, self.config.matching)

        for candidate in candidates[:3]:
            negotiation_id = str(uuid.uuid4())
            # The marketplace's own coarse-screen distance (straight-line), logged
            # alongside the optimiser's real road-network detour_km below so the
            # two can be compared directly -- see diagnose_veto_rate.py.
            screening_distance_km = haversine_km(truck.platform.current_location, candidate.public.origin)
            self.log.emit(
                time,
                EventType.MATCH_PROPOSED,
                negotiation_id,
                {
                    "truck_id": truck.truck_id,
                    "load_id": candidate.load_id,
                    "mechanism": self.mechanism.value,
                    "proposed_rate_per_km": candidate.public.posted_rate_per_km,
                    "is_backhaul": is_backhaul,
                    "screening_distance_km": screening_distance_km,
                },
            )
            result = verify_match(truck, candidate, self.network, time)
            if not result.ok:
                self.log.emit(
                    time,
                    EventType.MATCH_VETOED,
                    negotiation_id,
                    {
                        "truck_id": truck.truck_id,
                        "load_id": candidate.load_id,
                        "reason": result.reason.value,
                        "is_backhaul": is_backhaul,
                        "screening_distance_km": screening_distance_km,
                        "detour_km": result.detour_km,
                        "max_detour_km": truck.private.max_detour_km,
                    },
                )
                continue

            self.log.emit(
                time,
                EventType.MATCH_VERIFIED,
                negotiation_id,
                {
                    "truck_id": truck.truck_id,
                    "load_id": candidate.load_id,
                    "detour_km": result.detour_km,
                    "detour_min": result.detour_min,
                    "pickup_eta": result.pickup_eta.isoformat(),
                    "delivery_eta": result.delivery_eta.isoformat(),
                    "is_backhaul": is_backhaul,
                    "screening_distance_km": screening_distance_km,
                    "max_detour_km": truck.private.max_detour_km,
                },
            )
            self._create_and_execute_deal(truck, candidate, negotiation_id, time, result, is_backhaul)
            return candidate

        return None

    def _create_and_execute_deal(
        self, truck: Truck, load: Load, negotiation_id: str, depart_time: datetime, result, is_backhaul: bool
    ) -> None:
        deal_id = str(uuid.uuid4())
        commission_rate = self.config.matching.broker_commission_rate if self.mechanism == MatchMechanism.BROKER_COMMISSION else None
        deal = Deal(
            deal_id=deal_id,
            negotiation_id=negotiation_id,
            truck_id=truck.truck_id,
            load_id=load.load_id,
            mechanism=self.mechanism,
            agreed_rate_per_km=load.public.posted_rate_per_km,
            commission_rate=commission_rate,
            pickup_by=load.public.pickup_window[1],
            deliver_by=load.public.delivery_window[1],
            detour_km=result.detour_km,
            status=DealStatus.ACTIVE,
            created_at=depart_time,
        )
        self.deals[deal_id] = deal
        load.public.status = LoadStatus.MATCHED
        self.log.emit(
            depart_time,
            EventType.DEAL_CREATED,
            deal_id,
            {
                "negotiation_id": negotiation_id,
                "truck_id": truck.truck_id,
                "load_id": load.load_id,
                "mechanism": self.mechanism.value,
                "agreed_rate_per_km": deal.agreed_rate_per_km,
                "commission_rate": commission_rate,
                "pickup_by": deal.pickup_by.isoformat(),
                "deliver_by": deal.deliver_by.isoformat(),
                "detour_km": result.detour_km,
                "is_backhaul": is_backhaul,
            },
        )

        # Leg 1: reposition, empty, current location -> the load's origin
        origin, destination = load.public.origin, load.public.destination
        self.log.emit(
            depart_time,
            EventType.TRUCK_DEPARTED,
            truck.truck_id,
            {
                "purpose": DeparturePurpose.REPOSITION_TO_PICKUP.value,
                "deadhead_reason": None,
                "load_id": load.load_id,
                "from": _loc(truck.platform.current_location),
                "to": _loc(origin),
                "distance_km": result.detour_km,
                "expected_duration_min": result.detour_min,
                "laden": False,
                "is_backhaul": is_backhaul,
            },
        )
        pickup_arrival = depart_time + timedelta(minutes=result.detour_min)
        self.log.emit(
            pickup_arrival,
            EventType.TRUCK_ARRIVED,
            truck.truck_id,
            {"location": _loc(origin), "distance_km": result.detour_km, "duration_min": result.detour_min},
        )

        pickup_time = result.pickup_eta
        loaded_departure = pickup_time + timedelta(minutes=load.public.loading_time_min)

        # Leg 2: the paid trip, laden, origin -> destination
        leg2_km = self.network.distance_km(origin, destination)
        leg2_min = self.network.travel_time_min(origin, destination, loaded_departure)
        truck.platform.spare_capacity_weight_kg = truck.public.capacity_weight_kg - load.public.weight_kg
        truck.platform.spare_capacity_volume_m3 = truck.public.capacity_volume_m3 - load.public.volume_m3
        self.log.emit(
            loaded_departure,
            EventType.TRUCK_DEPARTED,
            truck.truck_id,
            {
                "purpose": DeparturePurpose.DELIVERY.value,
                "deadhead_reason": None,
                "load_id": load.load_id,
                "from": _loc(origin),
                "to": _loc(destination),
                "distance_km": leg2_km,
                "expected_duration_min": leg2_min,
                "laden": True,
            },
        )
        delivery_arrival = loaded_departure + timedelta(minutes=leg2_min)
        self.log.emit(
            delivery_arrival,
            EventType.TRUCK_ARRIVED,
            truck.truck_id,
            {"location": _loc(destination), "distance_km": leg2_km, "duration_min": leg2_min},
        )

        completion_time = delivery_arrival + timedelta(minutes=load.public.unloading_time_min)
        revenue = leg2_km * deal.agreed_rate_per_km
        commission_paid = revenue * commission_rate if commission_rate else 0.0

        deal.status = DealStatus.COMPLETED
        load.public.status = LoadStatus.DELIVERED
        self.log.emit(
            completion_time,
            EventType.DEAL_COMPLETED,
            deal_id,
            {
                "truck_id": truck.truck_id,
                "load_id": load.load_id,
                "distance_km": leg2_km,
                "duration_min": leg2_min,
                "revenue": revenue,
                "commission_paid": commission_paid,
                "is_backhaul": is_backhaul,
            },
        )

        truck.platform.current_location = destination
        truck.platform.spare_capacity_weight_kg = truck.public.capacity_weight_kg
        truck.platform.spare_capacity_volume_m3 = truck.public.capacity_volume_m3
        truck.platform.available_from = completion_time
        truck.platform.status = TruckStatus.IDLE  # provisional; the next decision may set WAITING

        self._schedule(completion_time, "truck_available", {"truck_id": truck.truck_id})


def run_scenario(config: Config, network: RoadNetwork, mechanism: MatchMechanism, run_id: str, log_path: str | Path) -> None:
    Simulator(config, network, mechanism, run_id, log_path).run()

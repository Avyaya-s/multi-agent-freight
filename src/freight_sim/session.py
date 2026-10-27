"""Interactive-mode session orchestrator -- the second engine path described
in CLAUDE.md's direction change. Batch mode (simulator.py) is seeded,
deterministic, and unchanged; this path is a live, human-in-the-loop session
that is non-reproducible by definition and must never be confused with it.

One session = one visitor-configured agent (a TRUCKER or a SHIPPER) placed
among a small simulated fleet of LLM-driven counterparties. Unlike batch
mode's multi-day discrete-event clock, a session is a single-shot snapshot at
"now": screen candidates (PHYSICAL layer only -- PREFERENCE is what the
negotiation itself is, see negotiation.py), negotiate each one in turn, and
stop at whichever limit comes first (the per-session LLM-call cap, or
candidates exhausted). No deal ever executes its legs -- this is a sandbox,
no real cargo moves (see CLAUDE.md) -- so VERIFIED is the last automatic
status; ACTIVE/FAILED only happen via the visitor's own accept/reject.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from freight_sim.clock import EPOCH
from freight_sim.config import Config
from freight_sim.demand import generate_fleet, generate_loads_for_day
from freight_sim.events import EventType, NegotiationParty
from freight_sim.feasibility import FeasibilityResult, check_feasibility
from freight_sim.llm_backend import LLMBackend
from freight_sim.matching import propose_candidates, propose_truck_candidates
from freight_sim.models import (
    Deal,
    DealStatus,
    Load,
    LoadPrivate,
    LoadPublic,
    LoadStatus,
    MatchMechanism,
    Truck,
    TruckPlatform,
    TruckPrivate,
    TruckPublic,
    TruckStatus,
)
from freight_sim.negotiation import (
    LLMStrategy,
    build_shipper_context,
    build_trucker_context,
    run_negotiation,
)
from freight_sim.rate_limiter import RateLimiter
from freight_sim.straight_line_network import Network

EmitFn = Callable[[EventType, str, dict], None]


# --------------------------------------------------------------------------- #
# Visitor agent construction -- stands in for the onboarding form (build
# order step 4, not yet built). These shapes are what that form will
# eventually submit; until it exists, the API accepts them directly.
# --------------------------------------------------------------------------- #


@dataclass
class VisitorTruckInput:
    capacity_weight_kg: float
    capacity_volume_m3: float
    home_lat: float
    home_lon: float
    reservation_rate_per_km: float
    max_detour_km: float
    max_wait_hours: float
    fixed_cost_per_day: float
    variable_cost_per_km: float
    fuel_efficiency_kmpl: float
    variable_cost_per_hour_waiting: float
    return_leg_discount: float
    home_by: datetime | None = None


@dataclass
class VisitorLoadInput:
    weight_kg: float
    volume_m3: float
    origin_lat: float
    origin_lon: float
    destination_lat: float
    destination_lon: float
    posted_rate_per_km: float
    reservation_rate_per_km: float
    penalty_per_hour_late: float
    loading_time_min: float
    unloading_time_min: float


def build_visitor_truck(network: Network, data: VisitorTruckInput) -> Truck:
    home = network.locate(data.home_lat, data.home_lon)
    truck_id = f"visitor-truck-{uuid.uuid4().hex[:8]}"
    return Truck(
        public=TruckPublic(truck_id=truck_id, owner_id="visitor", capacity_weight_kg=data.capacity_weight_kg, capacity_volume_m3=data.capacity_volume_m3),
        platform=TruckPlatform(
            current_location=home,
            status=TruckStatus.IDLE,
            available_from=EPOCH,
            spare_capacity_weight_kg=data.capacity_weight_kg,
            spare_capacity_volume_m3=data.capacity_volume_m3,
        ),
        private=TruckPrivate(
            fixed_cost_per_day=data.fixed_cost_per_day,
            variable_cost_per_km=data.variable_cost_per_km,
            fuel_efficiency_kmpl=data.fuel_efficiency_kmpl,
            variable_cost_per_hour_waiting=data.variable_cost_per_hour_waiting,
            reservation_rate_per_km=data.reservation_rate_per_km,
            return_leg_discount=data.return_leg_discount,
            home_location=home,
            home_by=data.home_by,
            max_detour_km=data.max_detour_km,
            max_wait_hours=data.max_wait_hours,
        ),
    )


def build_visitor_load(network: Network, config: Config, data: VisitorLoadInput) -> Load:
    """Pickup/delivery windows are derived with generous slack (the same
    shape demand.py uses for simulated loads), not taken from the visitor --
    a too-tight window would fail PHYSICAL feasibility for reasons that have
    nothing to do with the rate negotiation this session exists to demo."""
    origin = network.locate(data.origin_lat, data.origin_lon)
    destination = network.locate(data.destination_lat, data.destination_lon)
    load_id = f"visitor-load-{uuid.uuid4().hex[:8]}"
    pickup_start = EPOCH
    pickup_end = pickup_start + timedelta(hours=config.demand.pickup_window_hours)
    travel_min = network.travel_time_min(origin, destination, pickup_start)
    delivery_start = pickup_start + timedelta(minutes=data.loading_time_min + travel_min)
    delivery_end = delivery_start + timedelta(hours=config.demand.delivery_slack_hours.max)
    return Load(
        public=LoadPublic(
            load_id=load_id,
            shipper_id="visitor",
            origin=origin,
            destination=destination,
            weight_kg=data.weight_kg,
            volume_m3=data.volume_m3,
            pickup_window=(pickup_start, pickup_end),
            delivery_window=(delivery_start, delivery_end),
            posted_rate_per_km=data.posted_rate_per_km,
            loading_time_min=data.loading_time_min,
            unloading_time_min=data.unloading_time_min,
            posted_at=pickup_start,
            status=LoadStatus.OPEN,
        ),
        private=LoadPrivate(reservation_rate_per_km=data.reservation_rate_per_km, penalty_per_hour_late=data.penalty_per_hour_late),
    )


def _home_deadline_ok(truck_private: TruckPrivate, load: Load, physical: FeasibilityResult, network: Network) -> bool | None:
    """Same arithmetic as feasibility.check_preference's home_by check, kept
    separate because in interactive mode negotiation.py's hard veto (not
    check_preference) is the preference layer -- see its module docstring."""
    if truck_private.home_by is None:
        return None
    completion_time = physical.delivery_eta + timedelta(minutes=load.public.unloading_time_min)
    home_leg_min = network.travel_time_min(load.public.destination, truck_private.home_location, completion_time)
    return completion_time + timedelta(minutes=home_leg_min) <= truck_private.home_by


@dataclass
class _Candidate:
    truck: Truck
    load: Load
    physical: FeasibilityResult


class InteractiveSession:
    def __init__(
        self,
        session_id: str,
        config: Config,
        network: Network,
        backend: LLMBackend,
        role: NegotiationParty,
        visitor_truck: Truck | None = None,
        visitor_load: Load | None = None,
        rng_seed: int | None = None,
    ):
        if role == NegotiationParty.TRUCKER and visitor_truck is None:
            raise ValueError("role=TRUCKER requires visitor_truck")
        if role == NegotiationParty.SHIPPER and visitor_load is None:
            raise ValueError("role=SHIPPER requires visitor_load")

        self.session_id = session_id
        self.config = config
        self.network = network
        self.backend = backend
        self.role = role
        self.visitor_truck = visitor_truck
        self.visitor_load = visitor_load
        # None seeds from OS entropy -- a live session is non-reproducible by
        # design (see module docstring), unlike batch mode's config.run.seed.
        self.rng = random.Random(rng_seed)

        self.deals: dict[str, Deal] = {}
        self.llm_calls_used = 0
        self.ended_reason = "candidates_exhausted"

    # ------------------------------------------------------------------ #

    def run(self, emit: EmitFn) -> None:
        emit(EventType.SESSION_STARTED, self.session_id, {"role": self.role.value})

        counterparty_cfg = self.config.llm.counterparty
        visitor_cfg = self.config.llm.visitor
        counterparty_limiter = RateLimiter(counterparty_cfg.requests_per_minute, counterparty_cfg.requests_per_day, counterparty_cfg.tokens_per_minute)
        visitor_limiter = RateLimiter(visitor_cfg.requests_per_minute, visitor_cfg.requests_per_day, visitor_cfg.tokens_per_minute)

        candidates = self._screen_candidates(emit)
        for candidate in candidates:
            if self.llm_calls_used >= self.config.llm.max_llm_calls_per_session:
                self.ended_reason = "llm_call_cap_reached"
                break
            self._negotiate(candidate, emit, counterparty_limiter, visitor_limiter)

        emit(
            EventType.SESSION_ENDED,
            self.session_id,
            {"reason": self.ended_reason, "llm_calls_used": self.llm_calls_used, "deals_created": len(self.deals)},
        )

    # ------------------------------------------------------------------ #
    # candidate screening -- PHYSICAL layer only; PREFERENCE is negotiation
    # ------------------------------------------------------------------ #

    def _screen_candidates(self, emit: EmitFn) -> list[_Candidate]:
        """Trucks generated fresh for this session (via generate_loads_for_day
        for the demand side, or built directly for the visitor) always start
        at home, so is_backhaul is always False here -- a session is a
        single snapshot negotiation, not a multi-leg simulated schedule, so
        "already away from home needing a return leg" doesn't apply yet."""
        day_loads = generate_loads_for_day(self.config.demand, self.network, self.rng, day_index=0)
        pool_size = self.config.interactive.counterparty_fleet_size

        if self.role == NegotiationParty.TRUCKER:
            counterparty_loads = self.rng.sample(day_loads, k=min(pool_size, len(day_loads)))
            for load in counterparty_loads:
                self._emit_load_posted(emit, load)
            screened = propose_candidates(self.visitor_truck, counterparty_loads, self.config.matching)
            pairs = [(self.visitor_truck, load) for load in screened]
        else:
            counterparty_fleet_cfg = self.config.fleet.model_copy(update={"num_trucks": pool_size})
            counterparty_trucks = generate_fleet(counterparty_fleet_cfg, self.config.demand.zones, self.network, self.rng)
            for truck in counterparty_trucks:
                self._emit_truck_spawned(emit, truck)
            screened = propose_truck_candidates(self.visitor_load, counterparty_trucks, self.config.matching)
            pairs = [(truck, self.visitor_load) for truck in screened]

        candidates: list[_Candidate] = []
        for truck, load in pairs:
            [physical] = check_feasibility(truck.platform, [load], [], self.network, EPOCH, self.config.optimizer)
            negotiation_id = str(uuid.uuid4())
            emit(
                EventType.MATCH_PROPOSED,
                negotiation_id,
                {"truck_id": truck.truck_id, "load_id": load.load_id, "mechanism": MatchMechanism.NONE.value},
            )
            if not physical.feasible:
                emit(
                    EventType.MATCH_VETOED,
                    negotiation_id,
                    {"truck_id": truck.truck_id, "load_id": load.load_id, "reason": physical.reason.value, "layer": physical.layer.value},
                )
                continue
            emit(
                EventType.MATCH_VERIFIED,
                negotiation_id,
                {
                    "truck_id": truck.truck_id,
                    "load_id": load.load_id,
                    "detour_km": physical.detour_km,
                    "pickup_eta": physical.pickup_eta.isoformat(),
                    "delivery_eta": physical.delivery_eta.isoformat(),
                },
            )
            candidates.append(_Candidate(truck=truck, load=load, physical=physical))
        return candidates

    def _emit_load_posted(self, emit: EmitFn, load: Load) -> None:
        p = load.public
        emit(
            EventType.LOAD_POSTED,
            load.load_id,
            {
                "shipper_id": p.shipper_id,
                "origin": {"lat": p.origin.lat, "lon": p.origin.lon},
                "destination": {"lat": p.destination.lat, "lon": p.destination.lon},
                "weight_kg": p.weight_kg,
                "volume_m3": p.volume_m3,
                "posted_rate_per_km": p.posted_rate_per_km,
            },
        )

    def _emit_truck_spawned(self, emit: EmitFn, truck: Truck) -> None:
        emit(
            EventType.TRUCK_SPAWNED,
            truck.truck_id,
            {
                "owner_id": truck.public.owner_id,
                "capacity_weight_kg": truck.public.capacity_weight_kg,
                "capacity_volume_m3": truck.public.capacity_volume_m3,
                "spawn_location": {"lat": truck.platform.current_location.lat, "lon": truck.platform.current_location.lon},
            },
        )

    # ------------------------------------------------------------------ #
    # negotiation
    # ------------------------------------------------------------------ #

    def _negotiate(self, candidate: _Candidate, emit: EmitFn, counterparty_limiter: RateLimiter, visitor_limiter: RateLimiter) -> None:
        truck, load, physical = candidate.truck, candidate.load, candidate.physical
        max_rounds = self.config.llm.max_negotiation_rounds
        home_deadline_ok = _home_deadline_ok(truck.private, load, physical, self.network)

        trucker_context = build_trucker_context(truck, load, physical, is_backhaul=False, home_deadline_ok=home_deadline_ok, max_rounds=max_rounds)
        shipper_context = build_shipper_context(load, truck, physical, max_rounds=max_rounds)

        visitor_is_trucker = self.role == NegotiationParty.TRUCKER
        trucker_model, trucker_limiter = (self.config.llm.visitor.model, visitor_limiter) if visitor_is_trucker else (self.config.llm.counterparty.model, counterparty_limiter)
        shipper_model, shipper_limiter = (self.config.llm.counterparty.model, counterparty_limiter) if visitor_is_trucker else (self.config.llm.visitor.model, visitor_limiter)

        llm_cfg = self.config.llm
        trucker_strategy = LLMStrategy(self.backend, trucker_limiter, trucker_model, llm_cfg.timeout_seconds, llm_cfg.retry_attempts)
        shipper_strategy = LLMStrategy(self.backend, shipper_limiter, shipper_model, llm_cfg.timeout_seconds, llm_cfg.retry_attempts)

        negotiation_id = str(uuid.uuid4())

        def negotiation_emit(party: NegotiationParty, decision, round_number: int) -> None:
            self.llm_calls_used += (1 if decision.used_llm else 0) + decision.schema_failures
            turn = decision.turn
            emit(
                EventType.NEGOTIATION_MESSAGE,
                negotiation_id,
                {
                    "party": party.value,
                    "round": round_number,
                    "message_type": turn.message_type.value,
                    "rate_per_km": turn.rate_per_km,
                    "used_llm": decision.used_llm,
                    "used_fallback": decision.used_fallback,
                },
            )
            emit(EventType.AGENT_REASONING, negotiation_id, {"party": party.value, "round": round_number, "reasoning": turn.reasoning})

        result = run_negotiation(trucker_context, trucker_strategy, shipper_context, shipper_strategy, max_rounds, negotiation_emit)
        emit(
            EventType.NEGOTIATION_ENDED,
            negotiation_id,
            {"outcome": result.outcome.value, "rounds_used": result.rounds_used, "final_rate_per_km": result.final_rate_per_km},
        )

        if result.outcome.value != "accepted":
            return

        self._create_deal(truck, load, negotiation_id, physical, result.final_rate_per_km, emit)

    def _create_deal(self, truck: Truck, load: Load, negotiation_id: str, physical: FeasibilityResult, rate_per_km: float, emit: EmitFn) -> None:
        deal_id = str(uuid.uuid4())
        deal = Deal(
            deal_id=deal_id,
            negotiation_id=negotiation_id,
            truck_id=truck.truck_id,
            load_id=load.load_id,
            mechanism=MatchMechanism.NONE,
            agreed_rate_per_km=rate_per_km,
            commission_rate=None,
            pickup_by=load.public.pickup_window[1],
            deliver_by=load.public.delivery_window[1],
            detour_km=physical.detour_km,
            # The PHYSICAL layer already verified this candidate during
            # screening (_screen_candidates) and nothing geographic changed
            # during negotiation -- only price did -- so VERIFIED is correct
            # without re-solving. A sandbox deal never executes its legs, so
            # the visitor's own accept/reject (not simulated completion) is
            # what moves it to ACTIVE/FAILED next.
            status=DealStatus.VERIFIED,
            created_at=EPOCH,
        )
        self.deals[deal_id] = deal
        load.public.status = LoadStatus.MATCHED
        emit(
            EventType.DEAL_CREATED,
            deal_id,
            {
                "negotiation_id": negotiation_id,
                "truck_id": truck.truck_id,
                "load_id": load.load_id,
                "agreed_rate_per_km": rate_per_km,
                "detour_km": physical.detour_km,
                "pickup_by": deal.pickup_by.isoformat(),
                "deliver_by": deal.deliver_by.isoformat(),
            },
        )

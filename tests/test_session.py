"""Session orchestration tests use a backend that always fails (forcing the
deterministic RuleBasedStrategy fallback) or always returns a fixed valid
response -- real LLM conversational behavior is already covered live by
scripts/check_negotiation_quality.py and check_schema_reliability.py. These
tests are about session.py's own orchestration: screening, candidate
iteration, the per-session LLM-call cap, and deal creation -- not about
whether an LLM negotiates well."""

from freight_sim.events import EventType, NegotiationParty
from freight_sim.llm_backend import LLMCallError
from freight_sim.models import DealStatus
from freight_sim.session import (
    InteractiveSession,
    VisitorLoadInput,
    VisitorTruckInput,
    build_visitor_load,
    build_visitor_truck,
)

PEENYA = {"lat": 13.0284, "lon": 77.5199}


class AlwaysFailingBackend:
    """Forces every decision through to RuleBasedStrategy -- deterministic,
    well-tested in test_negotiation.py, no real API calls."""

    def complete(self, system_prompt, user_prompt, model, timeout_seconds):
        raise LLMCallError("forced failure for deterministic fallback in tests")


class ScriptedOfferBackend:
    """Always returns a valid, schema-passing "offer" -- enough real LLM
    decisions to exercise the per-session LLM-call cap deterministically
    (a negotiation that never terminates costs exactly max_rounds calls)."""

    def complete(self, system_prompt, user_prompt, model, timeout_seconds):
        from freight_sim.llm_backend import LLMResponse, LLMUsage

        return LLMResponse(
            raw_text="",
            parsed={"message_type": "offer", "rate_per_km": 12.0, "reasoning": "x"},
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=1.0,
            headers={},
        )


def _visitor_truck_input(**overrides) -> VisitorTruckInput:
    base = dict(
        capacity_weight_kg=20000.0,
        capacity_volume_m3=60.0,
        home_lat=PEENYA["lat"],
        home_lon=PEENYA["lon"],
        reservation_rate_per_km=5.0,  # deliberately low: guarantees acceptance by any shipper
        max_detour_km=200.0,
        max_wait_hours=24.0,
        fixed_cost_per_day=1500.0,
        variable_cost_per_km=10.0,
        fuel_efficiency_kmpl=4.5,
        variable_cost_per_hour_waiting=60.0,
        return_leg_discount=0.15,
    )
    base.update(overrides)
    return VisitorTruckInput(**base)


def _visitor_load_input(**overrides) -> VisitorLoadInput:
    base = dict(
        weight_kg=3000.0,
        volume_m3=10.0,
        origin_lat=PEENYA["lat"],
        origin_lon=PEENYA["lon"],
        destination_lat=12.9698,
        destination_lon=77.7500,
        posted_rate_per_km=20.0,  # deliberately high: guarantees acceptance by any trucker
        reservation_rate_per_km=25.0,
        penalty_per_hour_late=200.0,
        loading_time_min=30.0,
        unloading_time_min=30.0,
    )
    base.update(overrides)
    return VisitorLoadInput(**base)


def test_build_visitor_truck_reads_only_given_fields(fake_network):
    truck = build_visitor_truck(fake_network, _visitor_truck_input())
    assert truck.public.capacity_weight_kg == 20000.0
    assert truck.private.reservation_rate_per_km == 5.0
    assert truck.platform.current_location.lat == PEENYA["lat"]


def test_build_visitor_load_has_feasible_time_windows(fake_network, small_config):
    load = build_visitor_load(fake_network, small_config, _visitor_load_input())
    assert load.public.pickup_window[0] < load.public.pickup_window[1]
    assert load.public.delivery_window[0] < load.public.delivery_window[1]
    assert load.public.delivery_window[0] > load.public.pickup_window[0]  # delivery starts after loading + travel from pickup


def test_session_trucker_role_screens_and_can_produce_a_deal(fake_network, small_config):
    visitor_truck = build_visitor_truck(fake_network, _visitor_truck_input())
    session = InteractiveSession(
        session_id="s1",
        config=small_config,
        network=fake_network,
        backend=AlwaysFailingBackend(),
        role=NegotiationParty.TRUCKER,
        visitor_truck=visitor_truck,
        rng_seed=1,
    )
    events = []
    session.run(lambda event_type, entity_id, payload: events.append((event_type, entity_id, payload)))

    event_types = [e[0] for e in events]
    assert event_types[0] == EventType.SESSION_STARTED
    assert event_types[-1] == EventType.SESSION_ENDED
    # Confirmed with this seed/config: Peenya's zone alone posts several
    # loads/day within the screening radius, so at least one candidate
    # reaches negotiation. reservation_rate_per_km=5.0 (far below any
    # plausible posted rate) then guarantees RuleBasedStrategy's fallback
    # accepts it -- a screened candidate must produce a deal.
    assert any(et == EventType.MATCH_VERIFIED for et in event_types)
    assert len(session.deals) >= 1
    deal = next(iter(session.deals.values()))
    assert deal.status == DealStatus.VERIFIED


def test_session_shipper_role_mirrors_trucker_role(fake_network, small_config):
    visitor_load = build_visitor_load(fake_network, small_config, _visitor_load_input())
    session = InteractiveSession(
        session_id="s2",
        config=small_config,
        network=fake_network,
        backend=AlwaysFailingBackend(),
        role=NegotiationParty.SHIPPER,
        visitor_load=visitor_load,
        rng_seed=1,
    )
    events = []
    session.run(lambda event_type, entity_id, payload: events.append((event_type, entity_id, payload)))

    event_types = [e[0] for e in events]
    assert event_types[0] == EventType.SESSION_STARTED
    assert event_types[-1] == EventType.SESSION_ENDED
    assert any(et == EventType.TRUCK_SPAWNED for et in event_types)  # counterparty fleet, not loads, this time
    assert any(et == EventType.MATCH_VERIFIED for et in event_types)
    assert len(session.deals) >= 1


def test_session_stops_cleanly_at_the_llm_call_cap(fake_network, small_config):
    small_config.llm.max_llm_calls_per_session = 3  # below max_negotiation_rounds (4): the first negotiation alone exceeds it
    visitor_truck = build_visitor_truck(fake_network, _visitor_truck_input(max_detour_km=200.0))
    session = InteractiveSession(
        session_id="s3",
        config=small_config,
        network=fake_network,
        backend=ScriptedOfferBackend(),
        role=NegotiationParty.TRUCKER,
        visitor_truck=visitor_truck,
        rng_seed=1,
    )
    events = []
    session.run(lambda event_type, entity_id, payload: events.append((event_type, entity_id, payload)))

    session_ended = [p for et, eid, p in events if et == EventType.SESSION_ENDED][0]
    negotiations_ended = {eid for et, eid, _ in events if et == EventType.NEGOTIATION_ENDED}
    # Confirmed with this seed/config: at least 2 candidates screen feasible,
    # so the cap (3) binds after the first negotiation alone (4 calls, one
    # per round since it never terminates) exceeds it.
    assert len(negotiations_ended) == 1
    assert session.llm_calls_used >= small_config.llm.max_llm_calls_per_session
    assert session_ended["reason"] == "llm_call_cap_reached"
    # Never aborts mid-negotiation -- the cap is only checked between
    # candidates, so a negotiation that's already running is allowed to
    # finish, not cut off partway through.
    assert session.llm_calls_used == small_config.llm.max_negotiation_rounds

from dataclasses import dataclass
from datetime import datetime

from freight_sim.events import NegotiationMessageType, NegotiationOutcome, NegotiationParty
from freight_sim.llm_backend import LLMResponse, LLMSchemaError, LLMUsage, LLMRateLimitedError
from freight_sim.negotiation import (
    SYSTEM_PROMPT,
    LLMStrategy,
    NegotiationContext,
    NegotiationTurn,
    RuleBasedStrategy,
    build_shipper_context,
    build_trucker_context,
    build_user_prompt,
    run_negotiation,
)
from freight_sim.rate_limiter import RateLimiter
from test_models import make_load, make_truck


def floor_context(reservation=12.0, max_rounds=4, detour_km=None, max_detour_km=None, home_deadline_ok=None):
    return NegotiationContext(
        party=NegotiationParty.TRUCKER,
        reservation_rate_per_km=reservation,
        direction="floor",
        max_rounds=max_rounds,
        counterpart_summary="a load",
        physical_summary="a route",
        detour_km=detour_km,
        max_detour_km=max_detour_km,
        home_deadline_ok=home_deadline_ok,
    )


def ceiling_context(reservation=14.0, max_rounds=4):
    return NegotiationContext(
        party=NegotiationParty.SHIPPER,
        reservation_rate_per_km=reservation,
        direction="ceiling",
        max_rounds=max_rounds,
        counterpart_summary="a truck",
        physical_summary="a route",
    )


# ---------------------------------------------------------------------- #
# RuleBasedStrategy
# ---------------------------------------------------------------------- #


def test_rule_based_opens_with_offer_at_own_threshold():
    result = RuleBasedStrategy().decide(floor_context(reservation=12.0), round_number=1, incoming=None)
    assert result.turn.message_type == NegotiationMessageType.OFFER
    assert result.turn.rate_per_km == 12.0
    assert not result.used_llm and not result.used_fallback


def test_rule_based_accepts_when_threshold_met():
    incoming = NegotiationTurn(NegotiationMessageType.COUNTER, 13.0, "x")
    result = RuleBasedStrategy().decide(floor_context(reservation=12.0), round_number=2, incoming=incoming)
    assert result.turn.message_type == NegotiationMessageType.ACCEPT


def test_rule_based_counters_when_threshold_not_met_and_rounds_remain():
    incoming = NegotiationTurn(NegotiationMessageType.OFFER, 9.0, "x")
    result = RuleBasedStrategy().decide(floor_context(reservation=12.0, max_rounds=4), round_number=1, incoming=incoming)
    assert result.turn.message_type == NegotiationMessageType.COUNTER
    assert result.turn.rate_per_km == 12.0


def test_rule_based_rejects_when_out_of_rounds():
    incoming = NegotiationTurn(NegotiationMessageType.OFFER, 9.0, "x")
    result = RuleBasedStrategy().decide(floor_context(reservation=12.0, max_rounds=4), round_number=4, incoming=incoming)
    assert result.turn.message_type == NegotiationMessageType.REJECT


def test_rule_based_withdraws_on_detour_limit_regardless_of_rate():
    ctx = floor_context(reservation=12.0, detour_km=20.0, max_detour_km=10.0)
    result = RuleBasedStrategy().decide(ctx, round_number=1, incoming=None)
    assert result.turn.message_type == NegotiationMessageType.WITHDRAW


def test_rule_based_withdraws_on_home_deadline_regardless_of_rate():
    ctx = floor_context(reservation=12.0, home_deadline_ok=False)
    result = RuleBasedStrategy().decide(ctx, round_number=1, incoming=None)
    assert result.turn.message_type == NegotiationMessageType.WITHDRAW


# ---------------------------------------------------------------------- #
# LLMStrategy (fake backend -- no live API needed)
# ---------------------------------------------------------------------- #


class FakeBackend:
    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def complete(self, system_prompt, user_prompt, model, timeout_seconds):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_response(parsed: dict) -> LLMResponse:
    return LLMResponse(
        raw_text=str(parsed),
        parsed=parsed,
        usage=LLMUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        latency_ms=100.0,
        headers={},
    )


def make_limiter():
    return RateLimiter(requests_per_minute=1000, requests_per_day=1000, tokens_per_minute=100_000)


def test_llm_strategy_success_no_fallback():
    backend = FakeBackend([make_response({"message_type": "accept", "rate_per_km": 12.5, "reasoning": "fine"})])
    strategy = LLMStrategy(backend, make_limiter(), model="fake-model", timeout_seconds=5, retry_attempts=2)
    incoming = NegotiationTurn(NegotiationMessageType.COUNTER, 12.5, "x")
    result = strategy.decide(floor_context(), round_number=2, incoming=incoming)
    assert result.used_llm and not result.used_fallback
    assert result.turn.message_type == NegotiationMessageType.ACCEPT
    assert result.schema_failures == 0


def test_llm_strategy_falls_back_after_persistent_schema_failures():
    backend = FakeBackend([make_response({"bad": "shape"})] * 3)  # fails schema validation every time
    strategy = LLMStrategy(backend, make_limiter(), model="fake-model", timeout_seconds=5, retry_attempts=2)
    result = strategy.decide(floor_context(reservation=12.0), round_number=1, incoming=None)
    assert result.used_fallback
    assert result.schema_failures == 3
    assert result.turn.message_type == NegotiationMessageType.OFFER  # RuleBasedStrategy's opening move


def test_llm_strategy_recovers_after_one_schema_failure():
    backend = FakeBackend(
        [
            make_response({"bad": "shape"}),
            make_response({"message_type": "offer", "rate_per_km": 12.0, "reasoning": "ok"}),
        ]
    )
    strategy = LLMStrategy(backend, make_limiter(), model="fake-model", timeout_seconds=5, retry_attempts=2)
    result = strategy.decide(floor_context(), round_number=1, incoming=None)
    assert result.used_llm and not result.used_fallback
    assert result.schema_failures == 1


def test_llm_strategy_falls_back_on_persistent_rate_limiting():
    backend = FakeBackend([LLMRateLimitedError("429")] * 3)
    sleeps = []
    strategy = LLMStrategy(backend, make_limiter(), model="fake-model", timeout_seconds=5, retry_attempts=2, sleep_fn=sleeps.append)
    result = strategy.decide(floor_context(reservation=12.0), round_number=1, incoming=None)
    assert result.used_fallback
    assert result.was_rate_limited
    # backs off for real (exponentially) rather than hammering the API again
    # immediately -- the Stage 1 retry-storm lesson applied to a live API.
    assert sleeps == [1.0, 2.0]


def test_llm_strategy_retries_and_falls_back_on_opening_accept():
    """An "accept" (or "reject") on the opening move is meaningless -- there
    is no incoming offer to accept -- and must be treated like a schema
    failure, not passed through as a real deal."""
    backend = FakeBackend([make_response({"message_type": "accept", "rate_per_km": 12.0, "reasoning": "sure"})] * 3)
    strategy = LLMStrategy(backend, make_limiter(), model="fake-model", timeout_seconds=5, retry_attempts=2)
    result = strategy.decide(floor_context(reservation=12.0), round_number=1, incoming=None)
    assert result.used_fallback
    assert result.schema_failures == 3
    assert result.turn.message_type == NegotiationMessageType.OFFER  # RuleBasedStrategy's opening move


def test_llm_strategy_falls_back_immediately_when_daily_budget_exhausted():
    """RateLimitBudgetExhausted must be caught -- otherwise it propagates
    uncaught and crashes the negotiation instead of falling back, exactly the
    failure mode a rate limiter exists to prevent."""
    exhausted_limiter = RateLimiter(requests_per_minute=1000, requests_per_day=0, tokens_per_minute=100_000)
    backend = FakeBackend([])  # must never be called -- budget check happens first
    strategy = LLMStrategy(backend, exhausted_limiter, model="fake-model", timeout_seconds=5, retry_attempts=2, sleep_fn=lambda s: None)
    result = strategy.decide(floor_context(reservation=12.0), round_number=1, incoming=None)
    assert result.used_fallback
    assert result.was_rate_limited
    assert backend.calls == 0


# ---------------------------------------------------------------------- #
# run_negotiation
# ---------------------------------------------------------------------- #


def test_run_negotiation_reaches_acceptance_when_ranges_overlap():
    events = []
    result = run_negotiation(
        floor_context(reservation=11.0, max_rounds=4),
        RuleBasedStrategy(),
        ceiling_context(reservation=13.0, max_rounds=4),
        RuleBasedStrategy(),
        max_rounds=4,
        emit=lambda party, decision, round_number: events.append((party, decision.turn.message_type, round_number)),
    )
    assert result.outcome == NegotiationOutcome.ACCEPTED
    assert result.final_rate_per_km is not None
    assert len(events) == result.rounds_used


def test_run_negotiation_exceeds_max_rounds_when_ranges_do_not_overlap():
    result = run_negotiation(
        floor_context(reservation=20.0, max_rounds=4),  # wants >= 20
        RuleBasedStrategy(),
        ceiling_context(reservation=10.0, max_rounds=4),  # wants <= 10
        RuleBasedStrategy(),
        max_rounds=4,
        emit=lambda *a: None,
    )
    assert result.outcome in (NegotiationOutcome.MAX_ROUNDS_EXCEEDED, NegotiationOutcome.REJECTED)


def test_run_negotiation_trucker_withdraws_immediately_on_detour_limit():
    result = run_negotiation(
        floor_context(reservation=11.0, detour_km=50.0, max_detour_km=10.0),
        RuleBasedStrategy(),
        ceiling_context(reservation=13.0),
        RuleBasedStrategy(),
        max_rounds=4,
        emit=lambda *a: None,
    )
    assert result.outcome == NegotiationOutcome.WITHDRAWN
    assert result.rounds_used == 1


# ---------------------------------------------------------------------- #
# Context builders -- privacy boundary
# ---------------------------------------------------------------------- #


def test_build_trucker_context_never_reads_load_private(fake_network):
    truck = make_truck()
    load = make_load()
    load.private = _TripWire()
    from freight_sim.feasibility import check_feasibility
    from freight_sim.config import SolverConfig

    [physical] = check_feasibility(
        truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")
    )
    context = build_trucker_context(truck, load, physical, is_backhaul=False, home_deadline_ok=None, max_rounds=4)
    assert context.reservation_rate_per_km > 0  # didn't raise building it


def test_build_shipper_context_never_reads_truck_private(fake_network):
    truck = make_truck()
    load = make_load()
    truck.private = _TripWire()
    from freight_sim.feasibility import check_feasibility
    from freight_sim.config import SolverConfig

    truck_platform = truck.platform  # capture before poisoning .private
    [physical] = check_feasibility(
        truck_platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")
    )
    context = build_shipper_context(load, truck, physical, max_rounds=4)
    assert context.reservation_rate_per_km > 0  # didn't raise building it


class _TripWire:
    def __getattribute__(self, name):
        raise AssertionError(f"private field '{name}' was read by code that must not see it")


# ---------------------------------------------------------------------- #
# End-to-end: the actual rendered prompt text, not just the context builder
# ---------------------------------------------------------------------- #
#
# The tripwire tests above prove build_trucker_context/build_shipper_context
# don't *read* the other party's private fields. That's necessary but not
# sufficient: it doesn't prove the resulting prompt *string* -- the thing
# that actually reaches Groq -- stays clean. These tests plant a sentinel
# value in each private field and assert it never appears, by substring
# search, in SYSTEM_PROMPT + the rendered user prompt. This is what
# LLMStrategy.decide() sends verbatim as the system/user messages.


def _sentinel_load(fake_network):
    load = make_load()
    # Distinctive, never-coincidentally-matched values for every private field.
    load.private.reservation_rate_per_km = 918273.645
    load.private.penalty_per_hour_late = 273645.918
    return load


def _sentinel_truck():
    # Every numeric TruckPrivate field, including ones a prior version of this
    # test left at make_truck()'s default (e.g. max_wait_hours=12) -- those
    # defaults are short, ordinary-looking numbers ("12") that coincidentally
    # collide with legitimate prompt content like a clock time ("12:00") or a
    # rate ("12.0"), which produced a false positive here once. Every value
    # below is long and distinctive enough not to appear by accident.
    truck = make_truck()
    truck.private.fixed_cost_per_day = 112233.44
    truck.private.variable_cost_per_km = 998877.66
    truck.private.fuel_efficiency_kmpl = 554433.1
    truck.private.variable_cost_per_hour_waiting = 776655.44
    truck.private.reservation_rate_per_km = 554433.221
    truck.private.return_leg_discount = 0.123456789
    truck.private.max_detour_km = 667788.99
    truck.private.max_wait_hours = 445566.77
    return truck


def _sentinel_values(obj) -> list[str]:
    import dataclasses

    return [str(getattr(obj, f.name)) for f in dataclasses.fields(obj) if isinstance(getattr(obj, f.name), (int, float))]


def test_trucker_prompt_never_contains_shippers_private_values(fake_network):
    from freight_sim.config import SolverConfig
    from freight_sim.feasibility import check_feasibility

    truck = make_truck()
    load = _sentinel_load(fake_network)
    [physical] = check_feasibility(
        truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")
    )
    context = build_trucker_context(truck, load, physical, is_backhaul=False, home_deadline_ok=True, max_rounds=4)
    prompt = SYSTEM_PROMPT + "\n" + build_user_prompt(context, round_number=1, incoming=None)

    for value in _sentinel_values(load.private):
        assert value not in prompt, f"shipper private value {value} leaked into the trucker's prompt"


def test_shipper_prompt_never_contains_truckers_private_values(fake_network):
    from freight_sim.config import SolverConfig
    from freight_sim.feasibility import check_feasibility

    truck = _sentinel_truck()
    load = make_load()
    [physical] = check_feasibility(
        truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")
    )
    context = build_shipper_context(load, truck, physical, max_rounds=4)
    incoming = NegotiationTurn(NegotiationMessageType.OFFER, 12.0, "x")
    prompt = SYSTEM_PROMPT + "\n" + build_user_prompt(context, round_number=1, incoming=incoming)

    for value in _sentinel_values(truck.private):
        assert value not in prompt, f"trucker private value {value} leaked into the shipper's prompt"

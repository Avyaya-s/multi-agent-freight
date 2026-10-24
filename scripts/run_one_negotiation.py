"""Step 1 of the build order: get ONE negotiation working end to end and
logged before anything else. Uses the small counterparty model on both
sides (this is a plumbing smoke test, not the model-quality comparison --
see check_negotiation_quality.py for that) and a straight-line network
(the road network isn't what's being tested here).

Requires GROQ_API_KEY. Costs a handful of real API calls against the daily
budget -- see CLAUDE.md for why that budget is tight.
"""

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.events import EventLog, EventType, NegotiationOutcome
from freight_sim.feasibility import check_feasibility
from freight_sim.geo import haversine_km
from freight_sim.llm_backend import GroqBackend
from freight_sim.negotiation import (
    LLMStrategy,
    build_shipper_context,
    build_trucker_context,
    run_negotiation,
)
from freight_sim.negotiation_metrics import compute_negotiation_metrics
from freight_sim.rate_limiter import RateLimiter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from test_models import make_load, make_truck  # noqa: E402  (reuses the project's existing fixtures)

ROOT = Path(__file__).resolve().parent.parent


class _StraightLineNetwork:
    """A fast approximate network for exercising the negotiation path without
    paying for a real road-network warm-up -- the road network isn't what's
    being validated by this script."""

    def locate(self, lat, lon):
        from freight_sim.models import Location

        return Location(lat=lat, lon=lon)

    def distance_km(self, origin, destination):
        return haversine_km(origin, destination)

    def travel_time_min(self, origin, destination, depart_time):
        return self.distance_km(origin, destination) / 30.0 * 60.0


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    network = _StraightLineNetwork()
    truck = make_truck()
    load = make_load()

    [physical] = check_feasibility(truck.platform, [load], [], network, datetime(2024, 1, 1, 8), config.optimizer)
    if not physical.feasible:
        print(f"Not physically feasible ({physical.reason}) -- pick different fixtures.")
        return

    trucker_context = build_trucker_context(truck, load, physical, is_backhaul=False, home_deadline_ok=True, max_rounds=config.llm.max_negotiation_rounds)
    shipper_context = build_shipper_context(load, truck, physical, max_rounds=config.llm.max_negotiation_rounds)

    backend = GroqBackend()
    model = config.llm.counterparty.model
    rate_limiter = RateLimiter(
        requests_per_minute=config.llm.counterparty.requests_per_minute,
        requests_per_day=config.llm.counterparty.requests_per_day,
        tokens_per_minute=config.llm.counterparty.tokens_per_minute,
    )
    trucker_strategy = LLMStrategy(backend, rate_limiter, model, config.llm.timeout_seconds, config.llm.retry_attempts)
    shipper_strategy = LLMStrategy(backend, rate_limiter, model, config.llm.timeout_seconds, config.llm.retry_attempts)

    log_path = ROOT / "data" / "runs" / "one_negotiation.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()
    log = EventLog(log_path, run_id="one-negotiation-smoke-test")
    negotiation_id = "neg-smoke-001"

    def emit(party, decision, round_number):
        turn = decision.turn
        log.emit(
            datetime(2024, 1, 1, 8),
            EventType.NEGOTIATION_MESSAGE,
            negotiation_id,
            {
                "party": party.value,
                "round": round_number,
                "message_type": turn.message_type.value,
                "rate_per_km": turn.rate_per_km,
                "used_llm": decision.used_llm,
                "used_fallback": decision.used_fallback,
                "schema_failures": decision.schema_failures,
                "was_rate_limited": decision.was_rate_limited,
                "prompt_tokens": decision.usage.prompt_tokens if decision.usage else None,
                "completion_tokens": decision.usage.completion_tokens if decision.usage else None,
                "latency_ms": decision.latency_ms,
            },
        )
        log.emit(datetime(2024, 1, 1, 8), EventType.AGENT_REASONING, negotiation_id, {"party": party.value, "round": round_number, "reasoning": turn.reasoning})
        print(f"  round {round_number} [{party.value}] {turn.message_type.value} @ {turn.rate_per_km} -- {'LLM' if decision.used_llm else 'fallback' if decision.used_fallback else 'rule'}  ({turn.reasoning})")

    t0 = time.perf_counter()
    result = run_negotiation(trucker_context, trucker_strategy, shipper_context, shipper_strategy, config.llm.max_negotiation_rounds, emit)
    wall_clock_s = time.perf_counter() - t0

    log.emit(
        datetime(2024, 1, 1, 8),
        EventType.NEGOTIATION_ENDED,
        negotiation_id,
        {"outcome": result.outcome.value, "rounds_used": result.rounds_used, "final_rate_per_km": result.final_rate_per_km},
    )
    log.close()

    print(f"\nOutcome: {result.outcome.value}, rounds used: {result.rounds_used}, final rate: {result.final_rate_per_km}")
    print(f"Wall-clock time: {wall_clock_s:.2f}s")

    metrics = compute_negotiation_metrics(log_path)
    print(f"LLM decisions: {metrics.num_llm_decisions}, fallback decisions: {metrics.num_fallback_decisions}")
    print(f"Schema failures: {metrics.num_schema_failures}, rate-limited: {metrics.num_rate_limited_decisions}")
    print(f"Prompt tokens: {metrics.total_prompt_tokens}, completion tokens: {metrics.total_completion_tokens}")

    # Per-token cost is a rounding error at this volume -- the binding
    # constraint is the daily request cap, not dollars. What actually decides
    # whether "run it live" can ship is calls-per-negotiation -> sessions/day.
    real_requests = metrics.num_llm_decisions + metrics.num_schema_failures
    requests_per_negotiation = real_requests / metrics.num_negotiations if metrics.num_negotiations else 0.0
    rpd_budget = config.llm.counterparty.requests_per_day
    max_negotiations_per_day = rpd_budget / requests_per_negotiation if requests_per_negotiation else float("inf")
    print(f"\nReal API requests this negotiation: {real_requests} ({requests_per_negotiation:.1f} per negotiation)")
    print(f"At {rpd_budget:.0f} requests/day: ~{max_negotiations_per_day:.0f} negotiations/day possible (this role's model alone, no other traffic)")


if __name__ == "__main__":
    main()

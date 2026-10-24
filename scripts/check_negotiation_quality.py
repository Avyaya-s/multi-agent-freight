"""Check 2 before scaling past one negotiation: negotiation quality.

5 deliberately different scenarios per model (counterparty/small vs
visitor/mid) -- NOT the same scenario repeated. An earlier version of this
script reused the unit-test fixtures as-is, which only vary the ID, so all 5
"negotiations" were actually the identical economic situation five times --
that tests consistency under one scenario, not quality across a range of
difficulty, which is what this check is actually for. See CLAUDE.md.

The 5 scenarios span the cases that matter:
  - easy: wide rate overlap, should settle fast
  - narrow: barely any rate overlap, should still settle but may take rounds
  - impossible: reservation prices don't overlap at all -- correct behavior
    is REJECTED or MAX_ROUNDS_EXCEEDED, never an ACCEPT outside either
    party's own limit (that would be a rule violation)
  - realistic: a plausible real-world gap
  - detour_blocked: the truck's own max_detour_km is set below the actual
    (fixed) geography's detour, so the trucker should WITHDRAW immediately,
    never even negotiating rate

Report realised rate vs each side's reservation price, rounds to agreement,
and any rule violations (accepting outside reservation, or accepting despite
a detour/physical violation). If the small model just anchors on whatever
number it sees and concedes, the fleet needs a bigger model.

Requires GROQ_API_KEY. Costs up to ~5 x 2 x 4 x 2 = 80 real API calls against
the daily budget (5 scenarios, 2 models, up to 4 rounds, 2 parties) -- less
in practice since most negotiations end before the round cap.
"""

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import LLMRoleConfig, load_config
from freight_sim.feasibility import check_feasibility
from freight_sim.geo import haversine_km
from freight_sim.llm_backend import GroqBackend
from freight_sim.negotiation import (
    LLMStrategy,
    NegotiationContext,
    build_shipper_context,
    build_trucker_context,
    run_negotiation,
)
from freight_sim.rate_limiter import RateLimiter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from test_models import make_load, make_truck  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Scenario:
    name: str
    trucker_reservation: float
    shipper_reservation: float
    posted_rate: float
    max_detour_km: float  # the truck's own private limit; fixed geography gives ~7.85km detour


SCENARIOS = [
    Scenario("easy", trucker_reservation=10.0, shipper_reservation=18.0, posted_rate=14.0, max_detour_km=20.0),
    Scenario("narrow", trucker_reservation=13.5, shipper_reservation=14.5, posted_rate=14.0, max_detour_km=20.0),
    Scenario("impossible", trucker_reservation=16.0, shipper_reservation=13.0, posted_rate=14.0, max_detour_km=20.0),
    Scenario("realistic", trucker_reservation=9.0, shipper_reservation=16.0, posted_rate=12.0, max_detour_km=20.0),
    Scenario("detour_blocked", trucker_reservation=10.0, shipper_reservation=18.0, posted_rate=14.0, max_detour_km=1.0),
]


class _StraightLineNetwork:
    """Fast approximate network -- this check is about negotiation quality,
    not routing, so a real road-network warm-up would be wasted cost here."""

    def locate(self, lat, lon):
        from freight_sim.models import Location

        return Location(lat=lat, lon=lon)

    def distance_km(self, origin, destination):
        return haversine_km(origin, destination)

    def travel_time_min(self, origin, destination, depart_time):
        return self.distance_km(origin, destination) / 30.0 * 60.0


def build_scenario(scenario: Scenario, label: str, i: int):
    truck = make_truck(truck_id=f"truck-{label}-{i}")
    load = make_load(load_id=f"load-{label}-{i}")
    truck.private.reservation_rate_per_km = scenario.trucker_reservation
    truck.private.max_detour_km = scenario.max_detour_km
    load.private.reservation_rate_per_km = scenario.shipper_reservation
    load.public.posted_rate_per_km = scenario.posted_rate
    return truck, load


def check_violation(party_value: str, turn_rate: float | None, context: NegotiationContext) -> str | None:
    if turn_rate is None:
        return None
    if context.direction == "floor" and turn_rate < context.reservation_rate_per_km:
        return f"{party_value} accepted below its own reservation ({turn_rate} < {context.reservation_rate_per_km})"
    if context.direction == "ceiling" and turn_rate > context.reservation_rate_per_km:
        return f"{party_value} accepted above its own reservation ({turn_rate} > {context.reservation_rate_per_km})"
    return None


def run_batch(model: str, role_config: LLMRoleConfig, config, network, label: str) -> list[dict]:
    backend = GroqBackend()
    limiter = RateLimiter(
        requests_per_minute=role_config.requests_per_minute,
        requests_per_day=role_config.requests_per_day,
        tokens_per_minute=role_config.tokens_per_minute,
    )

    rows = []
    for i, scenario in enumerate(SCENARIOS):
        truck, load = build_scenario(scenario, label, i)
        [physical] = check_feasibility(truck.platform, [load], [], network, datetime(2024, 1, 1, 8), config.optimizer)
        if not physical.feasible:
            print(f"  [{label}] {scenario.name}: skipped (not physically feasible: {physical.reason})")
            continue

        trucker_context = build_trucker_context(truck, load, physical, is_backhaul=False, home_deadline_ok=True, max_rounds=config.llm.max_negotiation_rounds)
        shipper_context = build_shipper_context(load, truck, physical, max_rounds=config.llm.max_negotiation_rounds)
        trucker_strategy = LLMStrategy(backend, limiter, model, config.llm.timeout_seconds, config.llm.retry_attempts)
        shipper_strategy = LLMStrategy(backend, limiter, model, config.llm.timeout_seconds, config.llm.retry_attempts)

        violations: list[str] = []

        def emit(party, decision, round_number, violations=violations) -> None:
            turn = decision.turn
            if turn.message_type.value == "accept":
                ctx = trucker_context if party.value == "trucker" else shipper_context
                v = check_violation(party.value, turn.rate_per_km, ctx)
                if v:
                    violations.append(v)

        result = run_negotiation(trucker_context, trucker_strategy, shipper_context, shipper_strategy, config.llm.max_negotiation_rounds, emit)

        # "impossible" scenario: an ACCEPT at all is itself a violation --
        # there is no rate that satisfies both reservations simultaneously.
        if scenario.name == "impossible" and result.outcome.value == "accepted":
            violations.append(f"accepted a deal (rate={result.final_rate_per_km}) despite no rate satisfying both reservations")

        # "detour_blocked" scenario: anything other than an immediate
        # trucker withdrawal means the detour limit was ignored.
        if scenario.name == "detour_blocked" and result.outcome.value != "withdrawn":
            violations.append(f"did not withdraw despite detour exceeding the truck's own max_detour_km (outcome={result.outcome.value})")

        print(
            f"  [{label}] {scenario.name}: {result.outcome.value}, rounds={result.rounds_used}, "
            f"rate={result.final_rate_per_km}, trucker_reservation={trucker_context.reservation_rate_per_km:.2f}, "
            f"shipper_reservation={shipper_context.reservation_rate_per_km:.2f}, violations={violations}"
        )
        rows.append(
            {
                "scenario": scenario.name,
                "outcome": result.outcome.value,
                "rounds": result.rounds_used,
                "final_rate": result.final_rate_per_km,
                "trucker_reservation": trucker_context.reservation_rate_per_km,
                "shipper_reservation": shipper_context.reservation_rate_per_km,
                "violations": violations,
            }
        )

    return rows


def summarize(label: str, rows: list[dict]) -> None:
    accepted = [r for r in rows if r["outcome"] == "accepted"]
    mean_rounds = sum(r["rounds"] for r in rows) / len(rows) if rows else 0.0
    all_violations = [(r["scenario"], v) for r in rows for v in r["violations"]]
    print(f"\n{label}: {len(accepted)}/{len(rows)} accepted, mean rounds={mean_rounds:.1f}, violations={len(all_violations)}")
    for r in rows:
        surplus_capture = None
        if r["final_rate"] is not None:
            span = r["shipper_reservation"] - r["trucker_reservation"]
            if span > 0:
                surplus_capture = (r["final_rate"] - r["trucker_reservation"]) / span
        print(f"    [{r['scenario']}] outcome={r['outcome']}, rate={r['final_rate']}, trucker share of surplus={surplus_capture}")
    for scenario_name, v in all_violations:
        print(f"    VIOLATION [{scenario_name}]: {v}")


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    network = _StraightLineNetwork()

    print(f"=== {len(SCENARIOS)} scenarios: counterparty model ({config.llm.counterparty.model}) ===")
    small_rows = run_batch(config.llm.counterparty.model, config.llm.counterparty, config, network, "small")

    print(f"\n=== {len(SCENARIOS)} scenarios: visitor model ({config.llm.visitor.model}) ===")
    mid_rows = run_batch(config.llm.visitor.model, config.llm.visitor, config, network, "mid")

    summarize(config.llm.counterparty.model, small_rows)
    summarize(config.llm.visitor.model, mid_rows)


if __name__ == "__main__":
    main()

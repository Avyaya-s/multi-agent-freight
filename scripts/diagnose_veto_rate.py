"""Why does DETOUR_LIMIT account for ~77% of all vetoes?

Two competing hypotheses:
  (a) the coarse marketplace screen (straight-line distance <=
      matching.max_candidate_detour_km) proposes pairings that real road
      distance was never going to let through, regardless of the truck's
      own threshold -- i.e. the screen is too loose relative to how much
      real roads inflate straight-line distance in this network.
  (b) fleet.max_detour_km in config is just too tight for the physical scale
      of this specific road network (zones up to ~50km apart by real road).

This runs rule-based once and reports, for every DETOUR_LIMIT veto: the
coarse screening_distance_km (straight-line, what the marketplace saw), the
real detour_km computed by the optimiser, the inflation factor between them,
and the truck's own max_detour_km. It also reports the same inflation factor
for successful (MATCH_VERIFIED) matches, since if real/straight-line
inflation is similarly large there too, that confirms (a): the screen isn't
discriminating at all, just deferring the real check to verification.
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.events import EventType, read_events
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario

ROOT = Path(__file__).resolve().parent.parent


def summarize(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: (none)")
        return
    print(
        f"  {label}: n={len(values)}, mean={statistics.mean(values):.1f}, "
        f"median={statistics.median(values):.1f}, min={min(values):.1f}, max={max(values):.1f}"
    )


def main() -> None:
    config, config_hash = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(config.network)

    log_path = ROOT / "data" / "runs" / "diagnose_veto_rate.jsonl"
    if log_path.exists():
        log_path.unlink()
    run_scenario(config, network, MatchMechanism.RULE_BASED, f"diagnose-veto-seed{config.run.seed}-{config_hash}", log_path)

    detour_limit_screening = []
    detour_limit_real = []
    detour_limit_max = []
    detour_limit_inflation = []

    verified_screening = []
    verified_real = []
    verified_inflation = []

    for e in read_events(log_path):
        if e.event_type == EventType.MATCH_VETOED and e.payload["reason"] == "detour_limit":
            screening = e.payload["screening_distance_km"]
            real = e.payload["detour_km"]
            detour_limit_screening.append(screening)
            detour_limit_real.append(real)
            detour_limit_max.append(e.payload["max_detour_km"])
            if screening > 0:
                detour_limit_inflation.append(real / screening)

        elif e.event_type == EventType.MATCH_VERIFIED:
            screening = e.payload["screening_distance_km"]
            real = e.payload["detour_km"]
            verified_screening.append(screening)
            verified_real.append(real)
            if screening > 0:
                verified_inflation.append(real / screening)

    print("=== DETOUR_LIMIT vetoes ===")
    summarize("screening_distance_km (straight-line, coarse screen)", detour_limit_screening)
    summarize("detour_km (real road distance, optimiser)", detour_limit_real)
    summarize("max_detour_km (truck's private threshold)", detour_limit_max)
    summarize("inflation factor (real / straight-line)", detour_limit_inflation)

    print("\n=== successful matches (MATCH_VERIFIED) ===")
    summarize("screening_distance_km", verified_screening)
    summarize("detour_km (real)", verified_real)
    summarize("inflation factor (real / straight-line)", verified_inflation)

    print(f"\nmarketplace's coarse screen threshold (config): {config.matching.max_candidate_detour_km} km")
    print(f"fleet max_detour_km configured range: {config.fleet.max_detour_km.min}-{config.fleet.max_detour_km.max} km")

    if detour_limit_inflation and verified_inflation:
        mean_inflation_vetoed = statistics.mean(detour_limit_inflation)
        mean_inflation_verified = statistics.mean(verified_inflation)
        print(f"\nmean inflation, vetoed candidates: {mean_inflation_vetoed:.2f}x")
        print(f"mean inflation, verified candidates: {mean_inflation_verified:.2f}x")
        implied_effective_screen_km = config.matching.max_candidate_detour_km * mean_inflation_vetoed
        print(
            f"\nAt this inflation factor, a {config.matching.max_candidate_detour_km}km straight-line screen "
            f"corresponds to ~{implied_effective_screen_km:.0f}km of real road distance -- "
            f"compare to the fleet's max_detour_km range above."
        )


if __name__ == "__main__":
    main()

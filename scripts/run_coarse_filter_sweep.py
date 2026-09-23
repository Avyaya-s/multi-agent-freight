"""Is the 40% headline sensitive to the coarse filter's hand-tuned threshold?

The filter now sits upstream of the whole matching pipeline (recalibrated
25km -> 15km after the veto diagnosis), so before trusting it, sweep it and
check the headline empty-km reduction doesn't move much across a reasonable
range -- and report compute cost, since a looser filter's whole cost is more
(mostly wasted) verify_match calls.

Timing methodology: the route cache is cleared before each filter value's
*first* (timed) seed, so whichever value runs first doesn't unfairly warm the
cache for the ones after it. Two more seeds are run per value, untimed, to
get a robust mean empty-km reduction reusing the now-warm cache.
"""

import copy
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.events import EventType, read_events
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "runs"
RUNS.mkdir(parents=True, exist_ok=True)

FILTER_VALUES_KM = [10, 15, 20, 25]
SEEDS_PER_POINT = 3


def run_pair(config, network, seed: int):
    config = copy.deepcopy(config)
    config.run.seed = seed
    baseline_path = RUNS / "filter_sweep_baseline.jsonl"
    rule_based_path = RUNS / "filter_sweep_rule_based.jsonl"
    for p in (baseline_path, rule_based_path):
        if p.exists():
            p.unlink()
    run_scenario(config, network, MatchMechanism.NONE, "baseline", baseline_path)
    run_scenario(config, network, MatchMechanism.RULE_BASED, "rule_based", rule_based_path)
    return baseline_path, rule_based_path


def main() -> None:
    base_config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(base_config.network)

    rows = []
    for filter_km in FILTER_VALUES_KM:
        config = copy.deepcopy(base_config)
        config.matching.max_candidate_detour_km = filter_km

        network.clear_cache()
        t0 = time.perf_counter()
        baseline_path, rule_based_path = run_pair(config, network, base_config.run.seed)
        elapsed_cold = time.perf_counter() - t0

        baseline_metrics = compute_metrics(baseline_path)
        rule_based_metrics = compute_metrics(rule_based_path)
        num_proposed = sum(1 for e in read_events(rule_based_path) if e.event_type == EventType.MATCH_PROPOSED)

        reductions = []
        if baseline_metrics.empty_km:
            reductions.append((baseline_metrics.empty_km - rule_based_metrics.empty_km) / baseline_metrics.empty_km * 100)

        for i in range(1, SEEDS_PER_POINT):
            b_path, r_path = run_pair(config, network, base_config.run.seed + i)
            b_metrics = compute_metrics(b_path)
            r_metrics = compute_metrics(r_path)
            if b_metrics.empty_km:
                reductions.append((b_metrics.empty_km - r_metrics.empty_km) / b_metrics.empty_km * 100)

        mean_reduction = statistics.mean(reductions)
        rows.append(
            (filter_km, mean_reduction, statistics.stdev(reductions) if len(reductions) > 1 else 0.0, elapsed_cold, num_proposed, rule_based_metrics.num_match_vetoed)
        )
        print(
            f"filter={filter_km}km: mean_reduction={mean_reduction:.1f}% "
            f"(n={len(reductions)} seeds), cold_time={elapsed_cold:.1f}s, "
            f"match_proposed={num_proposed}, vetoes={rule_based_metrics.num_match_vetoed}"
        )

    print("\n| coarse filter (km) | mean empty-km reduction | stdev | cold-cache time (s) | MATCH_PROPOSED count | vetoes |")
    print("|---|---|---|---|---|---|")
    for filter_km, mean_reduction, stdev, elapsed, proposed, vetoes in rows:
        print(f"| {filter_km} | {mean_reduction:.1f}% | {stdev:.1f}pp | {elapsed:.1f} | {proposed} | {vetoes} |")


if __name__ == "__main__":
    main()

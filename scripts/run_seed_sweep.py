"""How much does the headline empty-km reduction vary with the random seed
alone? A single seed tells us nothing about variance -- this runs many and
reports the spread, not just the mean.
"""

import copy
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario

ROOT = Path(__file__).resolve().parent.parent
NUM_SEEDS = 20


def main() -> None:
    base_config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(base_config.network)

    reductions = []
    for i in range(NUM_SEEDS):
        seed = base_config.run.seed + i
        config = copy.deepcopy(base_config)
        config.run.seed = seed

        baseline_path = ROOT / "data" / "runs" / "seed_sweep_baseline.jsonl"
        rule_based_path = ROOT / "data" / "runs" / "seed_sweep_rule_based.jsonl"
        for p in (baseline_path, rule_based_path):
            if p.exists():
                p.unlink()

        run_scenario(config, network, MatchMechanism.NONE, f"seed{seed}-baseline", baseline_path)
        run_scenario(config, network, MatchMechanism.RULE_BASED, f"seed{seed}-rule_based", rule_based_path)

        baseline_metrics = compute_metrics(baseline_path)
        rule_based_metrics = compute_metrics(rule_based_path)

        pct_reduction = (
            (baseline_metrics.empty_km - rule_based_metrics.empty_km) / baseline_metrics.empty_km * 100
            if baseline_metrics.empty_km
            else 0.0
        )
        reductions.append(pct_reduction)
        print(f"seed {seed}: baseline empty_km={baseline_metrics.empty_km:.0f}, rule_based empty_km={rule_based_metrics.empty_km:.0f}, reduction={pct_reduction:.1f}%")

    print()
    print(f"n={len(reductions)}")
    print(f"mean reduction: {statistics.mean(reductions):.1f}%")
    print(f"stdev: {statistics.stdev(reductions):.1f}pp")
    print(f"min: {min(reductions):.1f}%  max: {max(reductions):.1f}%")


if __name__ == "__main__":
    main()

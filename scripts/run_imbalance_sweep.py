"""How sensitive is the headline empty-km reduction to the demand generator's
directional imbalance? Sweeps the industrial/consumption zones' dominant-
direction share from 0.5 (symmetric traffic, no imbalance at all) to 0.8 and
reports the marketplace's empty-km reduction at each point. If the reduction
were similarly large even at 0.5, that would mean the headline result has
little to do with the empty-return problem this project is actually about --
see README for the reading of this curve.
"""

import copy
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from freight_sim.config import Config, load_config
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario
from imbalance_sweep_utils import apply_skew

ROOT = Path(__file__).resolve().parent.parent
SKEWS = [0.5, 0.6, 0.7, 0.8]
SEEDS_PER_POINT = 3


def run_point(config: Config, network: RoadNetwork) -> float:
    reductions = []
    for i in range(SEEDS_PER_POINT):
        seed_config = copy.deepcopy(config)
        seed_config.run.seed = config.run.seed + i

        baseline_path = ROOT / "data" / "runs" / "imbalance_sweep_baseline.jsonl"
        rule_based_path = ROOT / "data" / "runs" / "imbalance_sweep_rule_based.jsonl"
        for p in (baseline_path, rule_based_path):
            if p.exists():
                p.unlink()

        run_scenario(seed_config, network, MatchMechanism.NONE, "baseline", baseline_path)
        run_scenario(seed_config, network, MatchMechanism.RULE_BASED, "rule_based", rule_based_path)

        baseline_metrics = compute_metrics(baseline_path)
        rule_based_metrics = compute_metrics(rule_based_path)
        if baseline_metrics.empty_km:
            reductions.append((baseline_metrics.empty_km - rule_based_metrics.empty_km) / baseline_metrics.empty_km * 100)

    return statistics.mean(reductions) if reductions else 0.0


def main() -> None:
    base_config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(base_config.network)

    rows = []
    for skew in SKEWS:
        config = apply_skew(base_config, skew)
        mean_reduction = run_point(config, network)
        rows.append((skew, mean_reduction))
        print(
            f"skew {skew:.1f} (dominant:secondary = {skew:.0%}:{1 - skew:.0%}): "
            f"mean empty-km reduction = {mean_reduction:.1f}% ({SEEDS_PER_POINT} seeds)"
        )

    print("\n| dominant-direction share | mean empty-km reduction |")
    print("|---|---|")
    for skew, reduction in rows:
        print(f"| {skew:.0%} : {1 - skew:.0%} | {reduction:.1f}% |")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot([s for s, _ in rows], [r for _, r in rows], marker="o")
    ax.set_xlabel("dominant-direction share of zone traffic (0.5 = symmetric)")
    ax.set_ylabel("mean empty-km reduction, marketplace vs baseline (%)")
    ax.set_title("Sensitivity of empty-km reduction to directional imbalance")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path = ROOT / "data" / "imbalance_sweep.png"
    fig.savefig(out_path, dpi=150)
    print(f"\nChart saved to {out_path}")


if __name__ == "__main__":
    main()

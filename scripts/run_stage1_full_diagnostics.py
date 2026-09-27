"""Full Stage 1 diagnostic sequence, run in one process (one route-cache
warm-up), all reflecting the retry-storm fix (a rejected (truck, load) pair
is never re-proposed):

  1. Main scenario comparison (rate check ON -- the real Stage 1 default).
  2. Ablation (rate check OFF): isolates "is OR-Tools stricter than
     arithmetic?" from "does the new rate check change the result?"
  3. 20-seed sweep (rate check ON): the headline number with variance.
  4. reservation_rate_per_km calibration sweep: how much does the headline
     number depend on this one hand-tuned overlap?
"""

import copy
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import Range, load_config
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import Simulator

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "runs"
RUNS.mkdir(parents=True, exist_ok=True)


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def run_pair(config, network, seed, check_rate, tag):
    config = copy.deepcopy(config)
    config.run.seed = seed
    baseline_path = RUNS / f"{tag}_baseline.jsonl"
    rule_based_path = RUNS / f"{tag}_rule_based.jsonl"
    for p in (baseline_path, rule_based_path):
        if p.exists():
            p.unlink()
    Simulator(config, network, MatchMechanism.NONE, f"{tag}-baseline", baseline_path, check_rate=check_rate).run()
    Simulator(config, network, MatchMechanism.RULE_BASED, f"{tag}-rule_based", rule_based_path, check_rate=check_rate).run()
    return compute_metrics(baseline_path), compute_metrics(rule_based_path)


def reduction_pct(baseline_metrics, rule_based_metrics) -> float:
    if not baseline_metrics.empty_km:
        return 0.0
    return (baseline_metrics.empty_km - rule_based_metrics.empty_km) / baseline_metrics.empty_km * 100


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(config.network)

    section("PART 1: main scenario comparison (rate check ON, retry-storm fix applied)")
    baseline_on, rule_on = run_pair(config, network, config.run.seed, True, "main_on")
    reduction_on = reduction_pct(baseline_on, rule_on)
    print(f"baseline: {baseline_on.as_row()}")
    print(f"rule_based: {rule_on.as_row()}")
    print(f"empty-km reduction: {reduction_on:.1f}%")
    print(f"veto_by_reason: {dict(rule_on.veto_by_reason)}")
    print(f"veto_by_layer: {dict(rule_on.veto_by_layer)}")
    print(f"veto_unique_pairs_by_reason: {dict(rule_on.veto_unique_pairs_by_reason)}")
    # With the retry-storm fix, every veto is now a unique pair by
    # construction, so these two should match exactly -- a good sanity check.
    assert dict(rule_on.veto_by_reason) == dict(rule_on.veto_unique_pairs_by_reason), "retry-storm fix did not eliminate repeats"
    print("Confirmed: veto_by_reason == veto_unique_pairs_by_reason (no repeats survive)")

    section("PART 2: ablation (rate check OFF -- isolates OR-Tools vs arithmetic)")
    baseline_off, rule_off = run_pair(config, network, config.run.seed, False, "ablation")
    reduction_off = reduction_pct(baseline_off, rule_off)
    print(f"empty-km reduction with rate check OFF: {reduction_off:.1f}%")
    print(f"empty-km reduction with rate check ON:  {reduction_on:.1f}%")
    print(f"veto_by_reason (rate OFF): {dict(rule_off.veto_by_reason)}")
    print(f"veto_by_layer (rate OFF): {dict(rule_off.veto_by_layer)}")

    section("PART 3: 20-seed sweep (rate check ON)")
    reductions = []
    for i in range(20):
        seed = config.run.seed + i
        b, r = run_pair(config, network, seed, True, "sweep20")
        pct = reduction_pct(b, r)
        reductions.append(pct)
        print(f"seed {seed}: reduction={pct:.1f}%")
    print(f"\nn={len(reductions)}, mean={statistics.mean(reductions):.1f}%, stdev={statistics.stdev(reductions):.1f}pp, "
          f"min={min(reductions):.1f}%, max={max(reductions):.1f}%")

    section("PART 4: reservation_rate_per_km calibration sweep")
    rate_ranges = [(6, 10), (8, 12), (9, 14), (11, 15), (12, 17)]
    seeds_per_point = 3
    rows = []
    for lo, hi in rate_ranges:
        point_config = copy.deepcopy(config)
        point_config.fleet.reservation_rate_per_km = Range(min=lo, max=hi)
        point_reductions = []
        rate_veto_shares = []
        for i in range(seeds_per_point):
            b, r = run_pair(point_config, network, config.run.seed + i, True, "ratesweep")
            point_reductions.append(reduction_pct(b, r))
            total_vetoes = sum(r.veto_by_reason.values())
            rate_veto_shares.append(r.veto_by_reason.get("rate_too_low", 0) / total_vetoes if total_vetoes else 0.0)
        mean_reduction = statistics.mean(point_reductions)
        mean_rate_share = statistics.mean(rate_veto_shares)
        rows.append((f"{lo}-{hi}", mean_reduction, mean_rate_share))
        print(f"reservation_rate_per_km {lo}-{hi} (posted rate 11-15): mean reduction={mean_reduction:.1f}%, mean rate-veto share={mean_rate_share:.1%}")

    print("\n| reservation_rate_per_km | mean empty-km reduction | mean rate-veto share |")
    print("|---|---|---|")
    for label, reduction, rate_share in rows:
        print(f"| {label} | {reduction:.1f}% | {rate_share:.1%} |")


if __name__ == "__main__":
    main()

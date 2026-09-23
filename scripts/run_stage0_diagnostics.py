"""Consolidated Stage 0 diagnostics, run in one process so the road-network
route cache warms once instead of once per script:

  1. main scenario comparison, with the recalibrated coarse-filter threshold
  2. veto diagnosis, to confirm the recalibration actually helped
  3. max_detour_km sensitivity sweep (hypothesis (b) from the veto diagnosis)
  4. zone-distribution imbalance sweep (does the exporter/importer gap widen
     with imbalance, even though the aggregate empty-km reduction didn't?)
"""

import copy
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from freight_sim.config import Range, load_config
from freight_sim.events import EventType, read_events
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario
from freight_sim.zone_metrics import compute_zone_metrics
from imbalance_sweep_utils import apply_skew

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "runs"
RUNS.mkdir(parents=True, exist_ok=True)


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def run_main_scenarios(config, network) -> Path:
    section("PART 1: main scenario comparison (recalibrated coarse filter)")
    scenarios = [
        ("baseline_no_marketplace", MatchMechanism.NONE),
        ("rule_based_marketplace", MatchMechanism.RULE_BASED),
        ("broker_commission", MatchMechanism.BROKER_COMMISSION),
    ]
    rows = []
    rule_based_log = None
    for name, mechanism in scenarios:
        log_path = RUNS / f"{name}.jsonl"
        if log_path.exists():
            log_path.unlink()
        run_scenario(config, network, mechanism, name, log_path)
        metrics = compute_metrics(log_path)
        rows.append({"scenario": name, **metrics.as_row()})
        print(f"{name}: veto_by_reason={dict(metrics.veto_by_reason)}")
        if mechanism == MatchMechanism.RULE_BASED:
            rule_based_log = log_path

    headers = list(rows[0].keys())
    print("\n| " + " | ".join(headers) + " |")
    print("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        print("| " + " | ".join(str(row[h]) for h in headers) + " |")

    return rule_based_log


def diagnose_vetoes(rule_based_log: Path) -> None:
    section("PART 2: veto diagnosis after recalibrated coarse filter")
    detour_limit_screening, detour_limit_real, detour_limit_max = [], [], []
    verified_screening, verified_real = [], []

    for e in read_events(rule_based_log):
        if e.event_type == EventType.MATCH_VETOED and e.payload["reason"] == "detour_limit":
            detour_limit_screening.append(e.payload["screening_distance_km"])
            detour_limit_real.append(e.payload["detour_km"])
            detour_limit_max.append(e.payload["max_detour_km"])
        elif e.event_type == EventType.MATCH_VERIFIED:
            verified_screening.append(e.payload["screening_distance_km"])
            verified_real.append(e.payload["detour_km"])

    def summarize(label: str, values: list[float]) -> None:
        if not values:
            print(f"  {label}: (none)")
            return
        print(f"  {label}: n={len(values)}, mean={statistics.mean(values):.1f}, median={statistics.median(values):.1f}")

    print("DETOUR_LIMIT vetoes:")
    summarize("screening_distance_km", detour_limit_screening)
    summarize("detour_km (real)", detour_limit_real)
    summarize("max_detour_km (truck threshold)", detour_limit_max)
    print("MATCH_VERIFIED (successful):")
    summarize("screening_distance_km", verified_screening)
    summarize("detour_km (real)", verified_real)


def run_max_detour_sweep(base_config, network) -> None:
    section("PART 3: max_detour_km sensitivity sweep")
    ranges = [(5, 15), (10, 30), (20, 40), (30, 50)]
    rows = []
    for lo, hi in ranges:
        config = copy.deepcopy(base_config)
        config.fleet.max_detour_km = Range(min=lo, max=hi)

        baseline_path = RUNS / "detour_sweep_baseline.jsonl"
        rule_based_path = RUNS / "detour_sweep_rule_based.jsonl"
        for p in (baseline_path, rule_based_path):
            if p.exists():
                p.unlink()

        run_scenario(config, network, MatchMechanism.NONE, "baseline", baseline_path)
        run_scenario(config, network, MatchMechanism.RULE_BASED, "rule_based", rule_based_path)

        baseline_metrics = compute_metrics(baseline_path)
        rule_based_metrics = compute_metrics(rule_based_path)
        reduction = (
            (baseline_metrics.empty_km - rule_based_metrics.empty_km) / baseline_metrics.empty_km * 100
            if baseline_metrics.empty_km
            else 0.0
        )
        veto_total = rule_based_metrics.num_match_vetoed
        detour_veto_share = rule_based_metrics.veto_by_reason.get("detour_limit", 0) / veto_total if veto_total else 0.0

        rows.append((f"{lo}-{hi}", reduction, rule_based_metrics.loaded_return_share, veto_total, detour_veto_share))
        print(
            f"max_detour_km {lo}-{hi}km: empty-km reduction={reduction:.1f}%, "
            f"loaded_return_share={rule_based_metrics.loaded_return_share:.3f}, "
            f"vetoes={veto_total} (detour_limit share={detour_veto_share:.0%})"
        )

    print("\n| max_detour_km range | empty-km reduction | loaded-return share | total vetoes | detour_limit share |")
    print("|---|---|---|---|---|")
    for label, reduction, share, vetoes, detour_share in rows:
        print(f"| {label} | {reduction:.1f}% | {share:.3f} | {vetoes} | {detour_share:.0%} |")


def run_zone_sweep(base_config, network) -> None:
    section("PART 4: zone-distribution imbalance sweep")
    skews = [0.5, 0.6, 0.7, 0.8]
    seeds_per_point = 3
    exporter, importer = "Peenya", "Whitefield"

    def fmt(x):
        return "n/a" if x is None else f"{x:.1%}"

    gap_rows = []
    for skew in skews:
        config = apply_skew(base_config, skew)
        log_paths = []
        for i in range(seeds_per_point):
            seed_config = copy.deepcopy(config)
            seed_config.run.seed = config.run.seed + i
            log_path = RUNS / f"zone_sweep_skew{skew}_seed{i}.jsonl"
            if log_path.exists():
                log_path.unlink()
            run_scenario(seed_config, network, MatchMechanism.RULE_BASED, f"zone-{skew}-{i}", log_path)
            log_paths.append(log_path)

        zm = compute_zone_metrics(log_paths, config.demand.zones)
        er, ir = zm.backhaul_success_rate_by_zone.get(exporter), zm.backhaul_success_rate_by_zone.get(importer)
        gap = (er - ir) if (er is not None and ir is not None) else None
        ee = zm.earnings_mean_by_home_zone.get(exporter, 0.0)
        ie = zm.earnings_mean_by_home_zone.get(importer, 0.0)

        print(f"\nskew {skew:.1f}:")
        print(f"  backhaul success rate by zone: {{ {', '.join(f'{z}: {fmt(r)}' for z, r in zm.backhaul_success_rate_by_zone.items())} }}")
        print(f"  load expiry rate by zone: {{ {', '.join(f'{z}: {fmt(r)}' for z, r in zm.load_expiry_rate_by_zone.items())} }}")
        print(f"  mean earnings by home zone: {{ {', '.join(f'{z}: {m:.0f}' for z, m in zm.earnings_mean_by_home_zone.items())} }}")
        print(f"  overall earnings mean={zm.overall_earnings_mean:.0f}, stdev={zm.overall_earnings_stdev:.0f}")

        gap_rows.append((skew, er, ir, gap, ee, ie))

    print(f"\nSUMMARY -- does the {exporter}-{importer} gap widen with imbalance?")
    print(f"| skew | {exporter} rate | {importer} rate | gap | {exporter} earnings | {importer} earnings |")
    print("|---|---|---|---|---|---|")
    for skew, er, ir, gap, ee, ie in gap_rows:
        print(f"| {skew:.0%} | {fmt(er)} | {fmt(ir)} | {fmt(gap)} | {ee:.0f} | {ie:.0f} |")


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(config.network)

    rule_based_log = run_main_scenarios(config, network)
    diagnose_vetoes(rule_based_log)
    run_max_detour_sweep(config, network)
    run_zone_sweep(config, network)


if __name__ == "__main__":
    main()

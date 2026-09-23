"""Distributional check on the imbalance-sweep finding.

run_imbalance_sweep.py found the *aggregate* empty-km reduction flat across
50:50 -> 80:20, which suggests the headline number is measuring generic
backhaul chaining rather than the directional-imbalance problem specifically.
Imbalance should show up in the *distribution*, not the mean: it should be
much harder to find a backhaul out of a net-importer zone than a net-exporter
zone, and that gap should widen as the configured imbalance increases.

PREDICTION (written down before running, per the point of this script): under
80:20, trucks stranded in Whitefield (net importer) should have a
substantially lower backhaul success rate than trucks stranded in Peenya
(net exporter), and that gap should shrink toward zero at 50:50. Earnings for
trucks based in Whitefield should likewise lag those based in Peenya, with
the gap widest at 80:20. If the gap does *not* widen with imbalance, that is
evidence the simulator's matching isn't responding to directional imbalance
the way the project's premise assumes it should.
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from freight_sim.config import load_config
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario
from freight_sim.zone_metrics import compute_zone_metrics
from imbalance_sweep_utils import apply_skew

ROOT = Path(__file__).resolve().parent.parent
SKEWS = [0.5, 0.6, 0.7, 0.8]
SEEDS_PER_POINT = 3
EXPORTER, IMPORTER = "Peenya", "Whitefield"


def fmt_rate(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def main() -> None:
    base_config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(base_config.network)

    print(__doc__)
    print("=" * 70)

    gap_rows = []
    for skew in SKEWS:
        config = apply_skew(base_config, skew)
        log_paths = []
        for i in range(SEEDS_PER_POINT):
            seed_config = config.model_copy(deep=True)
            seed_config.run.seed = config.run.seed + i
            log_path = ROOT / "data" / "runs" / f"zone_sweep_skew{skew}_seed{i}.jsonl"
            if log_path.exists():
                log_path.unlink()
            run_scenario(seed_config, network, MatchMechanism.RULE_BASED, f"zone-skew{skew}-seed{i}", log_path)
            log_paths.append(log_path)

        zm = compute_zone_metrics(log_paths, config.demand.zones)

        exporter_rate = zm.backhaul_success_rate_by_zone.get(EXPORTER)
        importer_rate = zm.backhaul_success_rate_by_zone.get(IMPORTER)
        gap = (exporter_rate - importer_rate) if (exporter_rate is not None and importer_rate is not None) else None

        exporter_earn = zm.earnings_mean_by_home_zone.get(EXPORTER, 0.0)
        importer_earn = zm.earnings_mean_by_home_zone.get(IMPORTER, 0.0)

        print(f"\n--- skew {skew:.1f} ({skew:.0%} dominant : {1 - skew:.0%} secondary) ---")
        print("backhaul success rate by zone (stranded-there -> loaded backhaul vs deadhead):")
        for zone, rate in zm.backhaul_success_rate_by_zone.items():
            print(f"  {zone}: {fmt_rate(rate)}")
        print(f"  gap ({EXPORTER} - {IMPORTER}): {fmt_rate(gap) if gap is not None else 'n/a'}")

        print("load expiry rate by origin zone:")
        for zone, rate in zm.load_expiry_rate_by_zone.items():
            print(f"  {zone}: {fmt_rate(rate)}")

        print("mean trucker earnings by home zone:")
        for zone, mean in zm.earnings_mean_by_home_zone.items():
            stdev = zm.earnings_stdev_by_home_zone[zone]
            print(f"  {zone}: mean={mean:.0f}, stdev={stdev:.0f}")
        print(f"overall earnings: mean={zm.overall_earnings_mean:.0f}, stdev={zm.overall_earnings_stdev:.0f}")

        gap_rows.append((skew, exporter_rate, importer_rate, gap, exporter_earn, importer_earn))

    print("\n" + "=" * 70)
    print("SUMMARY: does the exporter-importer gap widen with imbalance?\n")
    print(f"| skew | {EXPORTER} backhaul rate | {IMPORTER} backhaul rate | gap | {EXPORTER} earnings | {IMPORTER} earnings |")
    print("|---|---|---|---|---|---|")
    for skew, er, ir, gap, ee, ie in gap_rows:
        print(f"| {skew:.0%} | {fmt_rate(er)} | {fmt_rate(ir)} | {fmt_rate(gap)} | {ee:.0f} | {ie:.0f} |")


if __name__ == "__main__":
    main()

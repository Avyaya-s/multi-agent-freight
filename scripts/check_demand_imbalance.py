"""Sanity check: does the demand generator actually produce the directional
imbalance the config asks for, per zone, over a full run?

This exists because a symmetric demand generator would make matching look
artificially easy and inflate the headline empty-km result -- see README.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.demand import generate_loads_for_day
from freight_sim.network import RoadNetwork

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    config, _ = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(config.network)
    rng = random.Random(config.run.seed)

    outbound_by_zone: dict[str, int] = {z.name: 0 for z in config.demand.zones}
    inbound_by_zone: dict[str, int] = {z.name: 0 for z in config.demand.zones}

    for day in range(config.run.sim_days):
        loads = generate_loads_for_day(config.demand, network, rng, day)
        for load in loads:
            origin_zone = min(config.demand.zones, key=lambda z: (z.lat - load.public.origin.lat) ** 2 + (z.lon - load.public.origin.lon) ** 2)
            dest_zone = min(config.demand.zones, key=lambda z: (z.lat - load.public.destination.lat) ** 2 + (z.lon - load.public.destination.lon) ** 2)
            outbound_by_zone[origin_zone.name] += 1
            inbound_by_zone[dest_zone.name] += 1

    print(f"Observed over {config.run.sim_days} simulated days (seed {config.run.seed}):\n")
    print("| zone | configured outbound/day (min-max) | configured inbound/day (min-max) | observed outbound total | observed inbound total | observed ratio (out:in) |")
    print("|---|---|---|---|---|---|")
    for zone in config.demand.zones:
        out_total = outbound_by_zone[zone.name]
        in_total = inbound_by_zone[zone.name]
        ratio = out_total / in_total if in_total else float("inf")
        print(
            f"| {zone.name} | {zone.outbound_loads_per_day.min:.0f}-{zone.outbound_loads_per_day.max:.0f} "
            f"| {zone.inbound_loads_per_day.min:.0f}-{zone.inbound_loads_per_day.max:.0f} "
            f"| {out_total} | {in_total} | {ratio:.2f} |"
        )


if __name__ == "__main__":
    main()

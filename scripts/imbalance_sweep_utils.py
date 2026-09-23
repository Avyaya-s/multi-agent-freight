"""Shared by run_imbalance_sweep.py and run_zone_imbalance_sweep.py so the two
sweeps vary the imbalance ratio the same way."""

import copy

from freight_sim.config import Config, Range

# Peenya and Bommasandra ship out more than they receive; Whitefield receives
# more than it ships. Hosur is a genuinely mixed zone and is left untouched by
# the sweep (stays ~symmetric regardless of skew).
DOMINANT_DIRECTION = {
    "Peenya": "outbound",
    "Bommasandra": "outbound",
    "Whitefield": "inbound",
}


def apply_skew(config: Config, skew: float) -> Config:
    config = copy.deepcopy(config)
    for zone in config.demand.zones:
        direction = DOMINANT_DIRECTION.get(zone.name)
        if direction is None:
            continue
        total = (
            (zone.outbound_loads_per_day.min + zone.outbound_loads_per_day.max) / 2
            + (zone.inbound_loads_per_day.min + zone.inbound_loads_per_day.max) / 2
        )
        if direction == "outbound":
            outbound, inbound = total * skew, total * (1 - skew)
        else:
            outbound, inbound = total * (1 - skew), total * skew
        # Deterministic counts (min == max) at each sweep point, so the curve
        # isolates the imbalance effect instead of also carrying day-to-day
        # demand-volume noise.
        zone.outbound_loads_per_day = Range(min=outbound, max=outbound)
        zone.inbound_loads_per_day = Range(min=inbound, max=inbound)
    return config

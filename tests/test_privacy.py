"""Guards the public/platform/private split that the whole project design
leans on. Two kinds of check:

  - structural: no field name is duplicated across tiers (a cheap sanity
    check that would catch a copy-paste mistake)
  - behavioral: the marketplace's coarse screen (propose_candidates) and both
    feasibility layers (check_feasibility, check_preference) are handed a
    tripwire object in place of the tier they must not read, and the test
    fails immediately if any attribute on it is ever accessed. This is worth
    having before Stage 2 introduces real agents, since it is much cheaper to
    catch a leak here than after agent code exists that depends on one.

    check_feasibility's signature only ever accepts a TruckPlatform, not a
    Truck, so it has no way to structurally reach truck.private at all --
    a stronger guarantee than a tripwire can give. What both feasibility
    layers must still be proven not to touch is the *shipper's* private
    reservation price: a trucker's preference layer legitimately compares
    against the load's public posted rate, never the shipper's secret
    reservation price underneath it.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from freight_sim.config import MatchingConfig, SolverConfig
from freight_sim.feasibility import check_feasibility, check_preference
from freight_sim.matching import propose_candidates
from freight_sim.models import LoadPrivate, LoadPublic, TruckPlatform, TruckPrivate, TruckPublic
from test_models import make_load, make_truck

MATCHING_CFG = MatchingConfig(
    broker_commission_rate=0.10,
    max_candidate_detour_km=25,
    retry_interval_hours=1.0,
    home_deadline_buffer_hours=2.0,
)
SOLVER_CFG = SolverConfig(time_limit_ms=50, first_solution_strategy="PATH_CHEAPEST_ARC")


class TripWire:
    """Raises the moment any attribute is read, to prove the code holding
    this object never actually looks at it."""

    def __getattribute__(self, name):
        raise AssertionError(f"private field '{name}' was read by code that must not see it")


def test_truck_private_fields_do_not_overlap_public_or_platform_tiers():
    private_fields = {f.name for f in dataclasses.fields(TruckPrivate)}
    public_fields = {f.name for f in dataclasses.fields(TruckPublic)}
    platform_fields = {f.name for f in dataclasses.fields(TruckPlatform)}
    assert private_fields.isdisjoint(public_fields)
    assert private_fields.isdisjoint(platform_fields)


def test_load_private_fields_do_not_overlap_public_tier():
    private_fields = {f.name for f in dataclasses.fields(LoadPrivate)}
    public_fields = {f.name for f in dataclasses.fields(LoadPublic)}
    assert private_fields.isdisjoint(public_fields)


def test_propose_candidates_never_reads_truck_private(fake_network):
    """propose_candidates is what the marketplace can do -- it must only ever
    use public/platform fields."""
    truck = make_truck()
    load = make_load()
    truck.private = TripWire()
    candidates = propose_candidates(truck, [load], MATCHING_CFG)
    assert candidates == [load]


def test_check_feasibility_never_reads_load_private(fake_network):
    """The PHYSICAL layer checks time windows, capacity and routing -- it has
    no reason to know the shipper's true reservation price."""
    truck = make_truck()
    load = make_load()
    load.private = TripWire()
    [result] = check_feasibility(truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SOLVER_CFG)
    assert result.feasible


def test_check_preference_never_reads_load_private(fake_network):
    """The PREFERENCE layer legitimately reads the truck's own private
    thresholds (that's its whole job), but must compare against the load's
    *public* posted rate, never the shipper's private reservation price
    underneath it."""
    truck = make_truck()
    load = make_load()
    [physical] = check_feasibility(truck.platform, [load], [], fake_network, datetime(2024, 1, 1, 8), SOLVER_CFG)
    load.private = TripWire()
    result = check_preference(truck.private, load, physical, is_backhaul=False, network=fake_network)
    assert result.feasible

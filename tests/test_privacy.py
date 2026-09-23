"""Guards the public/platform/private split that the whole project design
leans on. Two kinds of check:

  - structural: no field name is duplicated across tiers (a cheap sanity
    check that would catch a copy-paste mistake)
  - behavioral: the marketplace's coarse screen (propose_candidates) and the
    optimiser's shipper-side view (verify_match) are handed a tripwire object
    in place of the tier they must not read, and the test fails immediately
    if any attribute on it is ever accessed. This is worth having before
    Stage 2 introduces real agents, since it is much cheaper to catch a leak
    here than after agent code exists that depends on one.
"""

from __future__ import annotations

import dataclasses

from freight_sim.config import MatchingConfig
from freight_sim.matching import propose_candidates, verify_match
from freight_sim.models import LoadPrivate, LoadPublic, TruckPlatform, TruckPrivate, TruckPublic
from test_models import make_load, make_truck

MATCHING_CFG = MatchingConfig(
    broker_commission_rate=0.10,
    max_candidate_detour_km=25,
    retry_interval_hours=1.0,
    home_deadline_buffer_hours=2.0,
)


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


def test_verify_match_never_reads_load_private(fake_network):
    """verify_match (the optimiser) legitimately needs the truck's private
    constraints (that's its whole job), but has no reason to know the
    shipper's true reservation price -- feasibility is about time/capacity/
    detour, not money."""
    from datetime import datetime

    truck = make_truck()
    load = make_load()
    load.private = TripWire()
    result = verify_match(truck, load, fake_network, datetime(2024, 1, 1, 8))
    assert result.ok

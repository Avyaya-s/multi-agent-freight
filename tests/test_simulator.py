from pathlib import Path

from freight_sim.events import EventType, read_events
from freight_sim.models import MatchMechanism
from freight_sim.simulator import Simulator


def run(config, fake_network, mechanism, tmp_path, name):
    log_path = tmp_path / f"{name}.jsonl"
    Simulator(config, fake_network, mechanism, run_id=name, log_path=log_path).run()
    return list(read_events(log_path))


def test_none_mechanism_dispatches_but_never_chains_a_backhaul(small_config, fake_network, tmp_path: Path):
    """Baseline trucks still get an ordinary job when idling at home (that's not
    the thing the marketplace changes), but once away from home they must
    always deadhead immediately rather than searching for a return load."""
    events = run(small_config, fake_network, MatchMechanism.NONE, tmp_path, "baseline")

    types = {e.event_type for e in events}
    assert EventType.TRUCK_WAIT_STARTED not in types  # baseline trucks never wait away from home
    assert EventType.SIM_STARTED in types
    assert EventType.SIM_ENDED in types
    assert EventType.TRUCK_SPAWNED in types
    assert EventType.TRUCK_IDLE_STARTED in types

    departures_by_truck: dict[str, list] = {}
    for e in events:
        if e.event_type == EventType.TRUCK_DEPARTED:
            departures_by_truck.setdefault(e.entity_id, []).append(e)

    for truck_departures in departures_by_truck.values():
        purposes = [d.payload["purpose"] for d in truck_departures]
        # A paid delivery leg is never immediately followed by another
        # reposition_to_pickup -- every "delivery" is followed by a deadhead.
        for i, purpose in enumerate(purposes):
            if purpose == "delivery" and i + 1 < len(purposes):
                assert purposes[i + 1] == "deadhead_home"


def test_rule_based_mechanism_produces_completed_deals(small_config, fake_network, tmp_path: Path):
    events = run(small_config, fake_network, MatchMechanism.RULE_BASED, tmp_path, "rule_based")

    created = [e for e in events if e.event_type == EventType.DEAL_CREATED]
    completed = [e for e in events if e.event_type == EventType.DEAL_COMPLETED]
    assert len(created) > 0
    # Stage 0 never fails a deal once created (no breakdowns/no-shows modeled yet)
    assert len(created) == len(completed)

    # every completed deal's truck actually did a laden departure
    laden_truck_ids = {e.entity_id for e in events if e.event_type == EventType.TRUCK_DEPARTED and e.payload["laden"]}
    for deal_event in completed:
        assert deal_event.payload["truck_id"] in laden_truck_ids


def test_broker_commission_reduces_trucker_earnings_vs_rule_based(small_config, fake_network, tmp_path: Path):
    from freight_sim.metrics import compute_metrics

    rule_path = tmp_path / "rule_based.jsonl"
    Simulator(small_config, fake_network, MatchMechanism.RULE_BASED, "rule_based", rule_path).run()
    rule_metrics = compute_metrics(rule_path)

    broker_path = tmp_path / "broker.jsonl"
    Simulator(small_config, fake_network, MatchMechanism.BROKER_COMMISSION, "broker", broker_path).run()
    broker_metrics = compute_metrics(broker_path)

    # Same seed -> same trucks/loads/timings, so shipper cost should match rupee-for-rupee...
    assert round(rule_metrics.shipper_cost, 2) == round(broker_metrics.shipper_cost, 2)
    # ...but the broker's commission comes out of the trucker's earnings, not the shipper's cost.
    assert broker_metrics.trucker_earnings < rule_metrics.trucker_earnings
    assert broker_metrics.broker_revenue > 0


def test_seq_is_monotonic_across_a_full_run(small_config, fake_network, tmp_path: Path):
    events = run(small_config, fake_network, MatchMechanism.RULE_BASED, tmp_path, "seq_check")
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))

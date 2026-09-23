from datetime import datetime
from pathlib import Path

from freight_sim.events import EventLog, EventType
from freight_sim.metrics import compute_metrics


def build_log(tmp_path: Path) -> Path:
    log_path = tmp_path / "run.jsonl"
    log = EventLog(log_path, run_id="test-run")
    t = datetime(2024, 1, 1, 8)

    log.emit(t, EventType.TRUCK_SPAWNED, "truck-1", {"fuel_efficiency_kmpl": 5.0})
    log.emit(t, EventType.TRUCK_SPAWNED, "truck-2", {"fuel_efficiency_kmpl": 4.0})
    log.emit(t, EventType.TRUCK_SPAWNED, "truck-3", {"fuel_efficiency_kmpl": 4.0})

    # truck-1: a genuine backhaul -- 10km empty reposition, then a 40km laden
    # delivery, completed for 400 revenue.
    log.emit(t, EventType.TRUCK_DEPARTED, "truck-1", {"distance_km": 10.0, "laden": False, "purpose": "reposition_to_pickup"})
    log.emit(t, EventType.TRUCK_DEPARTED, "truck-1", {"distance_km": 40.0, "laden": True, "purpose": "delivery"})
    log.emit(t, EventType.DEAL_COMPLETED, "deal-1", {"truck_id": "truck-1", "revenue": 400.0, "commission_paid": 40.0, "is_backhaul": True})

    # truck-2 and truck-3: both give up and deadhead home empty, no deal.
    log.emit(t, EventType.TRUCK_WAIT_ENDED, "truck-2", {"waited_min": 300.0})
    log.emit(t, EventType.TRUCK_DEPARTED, "truck-2", {"distance_km": 15.0, "laden": False, "purpose": "deadhead_home"})
    log.emit(t, EventType.TRUCK_IDLE_ENDED, "truck-2", {"idle_min": 120.0})

    log.emit(t, EventType.TRUCK_DEPARTED, "truck-3", {"distance_km": 5.0, "laden": False, "purpose": "deadhead_home"})

    log.emit(t, EventType.MATCH_VETOED, "neg-1", {"reason": "capacity", "is_backhaul": True})
    log.emit(t, EventType.MATCH_VETOED, "neg-2", {"reason": "capacity", "is_backhaul": True})
    log.emit(t, EventType.MATCH_VETOED, "neg-3", {"reason": "time_window", "is_backhaul": False})

    log.close()
    return log_path


def test_compute_metrics_aggregates_correctly(tmp_path: Path):
    metrics = compute_metrics(build_log(tmp_path))

    assert metrics.num_trucks == 3
    assert metrics.num_deals_completed == 1
    assert metrics.num_backhaul_deals_completed == 1
    assert metrics.num_deadhead_departures == 2
    assert metrics.num_match_vetoed == 3

    assert metrics.empty_km == 30.0  # 10 (reposition) + 15 + 5 (two deadheads)
    assert metrics.laden_km == 40.0
    assert metrics.total_km == 70.0

    # fuel: truck-1 drove 50km at 5 km/l = 10L; truck-2 15km + truck-3 5km at 4 km/l = 5L
    assert round(metrics.fuel_liters, 2) == 15.0

    assert round(metrics.waiting_hours, 2) == 5.0  # 300 min
    assert round(metrics.idle_hours, 2) == 2.0  # 120 min

    assert metrics.trucker_earnings == 360.0  # 400 - 40 commission
    assert metrics.broker_revenue == 40.0
    assert metrics.shipper_cost == 400.0

    # 1 backhaul deal, 2 deadheads -> a third of away-from-home decisions were loaded
    assert round(metrics.loaded_return_share, 4) == round(1 / 3, 4)

    assert metrics.veto_by_reason == {"capacity": 2, "time_window": 1}
    rates = metrics.veto_rate_by_reason()
    assert round(rates["capacity"], 4) == round(2 / 3, 4)
    assert round(rates["time_window"], 4) == round(1 / 3, 4)


def test_loaded_return_share_is_zero_with_no_backhauls_or_deadheads(tmp_path: Path):
    log_path = tmp_path / "empty.jsonl"
    log = EventLog(log_path, run_id="test-run")
    log.close()
    metrics = compute_metrics(log_path)
    assert metrics.loaded_return_share == 0.0

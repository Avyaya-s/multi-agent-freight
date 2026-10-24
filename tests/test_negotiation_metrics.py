from datetime import datetime
from pathlib import Path

from freight_sim.events import EventLog, EventType
from freight_sim.negotiation_metrics import compute_negotiation_metrics


def build_log(tmp_path: Path) -> Path:
    log_path = tmp_path / "negotiation.jsonl"
    log = EventLog(log_path, run_id="test-run")
    t = datetime(2024, 1, 1, 8)

    # negotiation-1: LLM succeeds first try, then falls back after a schema failure, reaches ACCEPT in 2 rounds
    log.emit(t, EventType.NEGOTIATION_MESSAGE, "neg-1", {"used_llm": True, "used_fallback": False, "schema_failures": 0, "prompt_tokens": 100, "completion_tokens": 20, "latency_ms": 200.0})
    log.emit(t, EventType.NEGOTIATION_MESSAGE, "neg-1", {"used_llm": False, "used_fallback": True, "schema_failures": 2, "was_rate_limited": False, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0.0})
    log.emit(t, EventType.NEGOTIATION_ENDED, "neg-1", {"outcome": "accepted", "rounds_used": 2, "final_rate_per_km": 12.5})

    # negotiation-2: rate-limited then falls back, ends in max_rounds_exceeded at round 4
    log.emit(t, EventType.NEGOTIATION_MESSAGE, "neg-2", {"used_llm": False, "used_fallback": True, "schema_failures": 0, "was_rate_limited": True, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0.0})
    log.emit(t, EventType.NEGOTIATION_ENDED, "neg-2", {"outcome": "max_rounds_exceeded", "rounds_used": 4, "final_rate_per_km": None})

    log.close()
    return log_path


def test_compute_negotiation_metrics(tmp_path: Path):
    metrics = compute_negotiation_metrics(build_log(tmp_path))

    assert metrics.num_negotiations == 2
    assert metrics.outcome_counts == {"accepted": 1, "max_rounds_exceeded": 1}
    assert metrics.rounds_used == [2, 4]
    assert round(metrics.mean_rounds, 2) == 3.0

    assert metrics.num_llm_decisions == 1
    assert metrics.num_fallback_decisions == 2
    assert round(metrics.fallback_rate, 4) == round(2 / 3, 4)

    assert metrics.num_schema_failures == 2
    # attempts = 1 (llm) + 2 (fallback) + 2 (schema failures) = 5
    assert round(metrics.schema_failure_rate, 4) == round(2 / 5, 4)

    assert metrics.num_rate_limited_decisions == 1
    assert metrics.total_prompt_tokens == 100
    assert metrics.total_completion_tokens == 20

    cost = metrics.estimate_cost(price_per_million_prompt=0.10, price_per_million_completion=0.50)
    assert round(cost, 8) == round(100 / 1_000_000 * 0.10 + 20 / 1_000_000 * 0.50, 8)


def test_empty_log_gives_zeroed_rates(tmp_path: Path):
    log_path = tmp_path / "empty.jsonl"
    EventLog(log_path, run_id="test-run").close()
    metrics = compute_negotiation_metrics(log_path)
    assert metrics.fallback_rate == 0.0
    assert metrics.schema_failure_rate == 0.0
    assert metrics.mean_rounds == 0.0

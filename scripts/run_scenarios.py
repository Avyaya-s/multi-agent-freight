"""Runs the Stage 0 baseline scenarios on identical trucks/loads/timings
(same seed) and prints a results table."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.metrics import compute_metrics
from freight_sim.models import MatchMechanism
from freight_sim.network import RoadNetwork
from freight_sim.simulator import run_scenario

ROOT = Path(__file__).resolve().parent.parent

SCENARIOS = [
    ("baseline_no_marketplace", MatchMechanism.NONE),
    ("rule_based_marketplace", MatchMechanism.RULE_BASED),
    ("broker_commission", MatchMechanism.BROKER_COMMISSION),
]


def main() -> None:
    config, config_hash = load_config(ROOT / "config" / "default.yaml")
    network = RoadNetwork.load(config.network)

    rows = []
    veto_breakdowns = []
    for name, mechanism in SCENARIOS:
        log_path = ROOT / "data" / "runs" / f"{name}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            log_path.unlink()
        run_id = f"{name}-seed{config.run.seed}-{config_hash}"
        run_scenario(config, network, mechanism, run_id, log_path)
        metrics = compute_metrics(log_path)
        rows.append({"scenario": name, **metrics.as_row()})
        veto_breakdowns.append((name, metrics.veto_by_reason, metrics.veto_rate_by_reason()))

    headers = list(rows[0].keys())
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        print("| " + " | ".join(str(row[h]) for h in headers) + " |")

    print("\nveto reasons by scenario:")
    for name, counts, rates in veto_breakdowns:
        breakdown = ", ".join(f"{reason}={count} ({rates[reason]:.0%})" for reason, count in counts.items())
        print(f"  {name}: {breakdown or '(no vetoes)'}")


if __name__ == "__main__":
    main()

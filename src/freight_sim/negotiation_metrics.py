"""Metrics computed from NEGOTIATION_MESSAGE / NEGOTIATION_ENDED events.

Kept separate from metrics.py (Stage 0/1's truck/load/deal metrics) since
Stage 2's negotiation data is a different shape entirely -- same principle as
zone_metrics.py being its own module rather than bloating Metrics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from freight_sim.events import EventType, read_events


@dataclass
class NegotiationMetrics:
    num_negotiations: int = 0
    outcome_counts: Counter = field(default_factory=Counter)
    rounds_used: list[int] = field(default_factory=list)
    num_llm_decisions: int = 0
    num_fallback_decisions: int = 0
    # Summed across all retry attempts, not deduplicated -- unlike the Stage 1
    # retry-storm fix, a schema failure here isn't a permanent fact about a
    # pair, just a per-call quality signal, so there's nothing to dedupe.
    num_schema_failures: int = 0
    num_rate_limited_decisions: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_latency_ms: float = 0.0

    @property
    def fallback_rate(self) -> float:
        """Of decisions actually backed by an LLMStrategy (succeeded or fell
        back), the fraction that fell back. Excludes decisions from a plain
        RuleBasedStrategy party, which never attempted an LLM call at all."""
        denom = self.num_llm_decisions + self.num_fallback_decisions
        return self.num_fallback_decisions / denom if denom else 0.0

    @property
    def schema_failure_rate(self) -> float:
        """Failures per LLM call attempt (not per decision -- a decision can
        involve several retried attempts, each of which may fail on schema).
        Separate from fallback_rate on purpose: a schema failure a retry then
        fixes never causes a fallback at all."""
        attempts = self.num_llm_decisions + self.num_fallback_decisions + self.num_schema_failures
        return self.num_schema_failures / attempts if attempts else 0.0

    @property
    def mean_rounds(self) -> float:
        return sum(self.rounds_used) / len(self.rounds_used) if self.rounds_used else 0.0

    def estimate_cost(self, price_per_million_prompt: float, price_per_million_completion: float) -> float:
        """Caller supplies current per-model pricing -- not hardcoded here,
        since it wasn't independently confirmed at build time. Verify against
        the Groq console before trusting a reported number."""
        return (
            self.total_prompt_tokens / 1_000_000 * price_per_million_prompt
            + self.total_completion_tokens / 1_000_000 * price_per_million_completion
        )


def compute_negotiation_metrics(log_path: str | Path) -> NegotiationMetrics:
    metrics = NegotiationMetrics()
    for event in read_events(Path(log_path)):
        if event.event_type == EventType.NEGOTIATION_MESSAGE:
            if event.payload.get("used_llm"):
                metrics.num_llm_decisions += 1
            if event.payload.get("used_fallback"):
                metrics.num_fallback_decisions += 1
            metrics.num_schema_failures += event.payload.get("schema_failures") or 0
            if event.payload.get("was_rate_limited"):
                metrics.num_rate_limited_decisions += 1
            metrics.total_prompt_tokens += event.payload.get("prompt_tokens") or 0
            metrics.total_completion_tokens += event.payload.get("completion_tokens") or 0
            metrics.total_latency_ms += event.payload.get("latency_ms") or 0.0
        elif event.event_type == EventType.NEGOTIATION_ENDED:
            metrics.num_negotiations += 1
            metrics.outcome_counts[event.payload["outcome"]] += 1
            metrics.rounds_used.append(event.payload["rounds_used"])
    return metrics
